from collections import Counter

from xdsl.ir import Operation, Block
from xdsl.dialects.builtin import ModuleOp
from xdsl.pattern_rewriter import (op_type_rewrite_pattern, RewritePattern,
                                   PatternRewriter)

import toy.dialect as td
import riscv.riscv_ssa as rd
import riscv_buffer_ir.dialect as rbd
import vector_ir.dialect as tvd


class AddSections(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: ModuleOp, rewriter: PatternRewriter):

        # bss stands for block starting symbol
        heap_section = rd.SectionOp.from_ops(
            '.bss',
            [
                rd.LabelOp.get("heap"),
                rd.DirectiveOp.get(".space", f'{1024}'),  # 1kb
            ])
        data_section = rd.SectionOp.from_ops('.data', [])
        text_section = rd.SectionOp.from_region(
            '.text',
            rewriter.move_region_contents_to_new_regions(op.regions[0]))

        op.body.add_block(
            Block.from_ops([heap_section, data_section, text_section]))


class LowerFuncOp(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: td.FuncOp, rewriter: PatternRewriter):
        name = op.sym_name.data

        # TODO: add support for user defined functions
        assert name == 'main', 'Only support lowering main function for now'

        region = op.regions[0]

        # insert a heap pointer at the start of every function
        rewriter.insert_op_before(rd.LIOp.get('heap'), region.blocks[0].ops[0])

        # create riscv func op with same ops
        riscv_op = rd.FuncOp.from_region(
            name, rewriter.move_region_contents_to_new_regions(region))

        rewriter.replace_matched_op(riscv_op)


class LowerReturnOp(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: td.ReturnOp, rewriter: PatternRewriter):
        # TODO: add support for optional argument
        assert op.input is None, 'Only support return with no arguments for now'

        rewriter.replace_matched_op(rd.ReturnOp.get())


class DataSectionRewritePattern(RewritePattern):

    _data_section: rd.SectionOp | None = None
    _counter: Counter[str] = Counter()

    def data_section(self, op: Operation) -> rd.SectionOp:
        '''
        Relies on the data secition being inserted earlier by AddDataSection
        '''
        if self._data_section is None:
            module_op = op.get_toplevel_object()
            assert isinstance(
                module_op, ModuleOp
            ), f'The top level object of {str(op)} must be a ModuleOp'

            for op in module_op.body.blocks[0].ops:
                if not isinstance(op, rd.SectionOp):
                    continue
                if op.directive.data != '.data':
                    continue
                self._data_section = op

            assert self._data_section is not None

        return self._data_section

    def label(self, func_name: str, kind: str) -> str:
        key = f'{func_name}.{kind}'
        count = self._counter[key]
        self._counter[key] += 1
        return f'{key}.{count}'

    def func_name_of_op(self, op: Operation) -> str:
        region = op.parent_region()
        assert region is not None
        func_op = region.parent_op()
        assert isinstance(func_op, rd.FuncOp)
        return func_op.func_name.data

    def add_data(self, op: Operation, label: str, data: list[int]):
        encoded_data = ', '.join(hex(el) for el in data)
        self.data_section(op).regions[0].blocks[0].add_ops(
            [rd.LabelOp.get(label),
             rd.DirectiveOp.get(".word", encoded_data)])


class LowerVectorConstantOp(DataSectionRewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: tvd.VectorConstantOp,
                          rewriter: PatternRewriter):
        """
        Vectors are represented in memory as an n+1 array of int32, where the first
        entry is the count of the vector
        """
        data = op.get_data()
        label = self.label(self.func_name_of_op(op), op.label.data)

        self.add_data(op, label, [len(data), *data])
        rewriter.replace_matched_op(rd.LIOp.get(label))


class LowerPrintOp(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: td.PrintOp, rewriter: PatternRewriter):
        rewriter.replace_matched_op(rbd.TensorPrintOp.get(op.input))


class LowerVectorAddOp(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: tvd.VectorAddOp,
                          rewriter: PatternRewriter):

        rewriter.replace_matched_op([
            count := rd.LWOp.get(op.rs1, 0, 'Get input count'),
            storage_count := rd.AddIOp.get(count, 1,
                                           'Input storage int32 count'),
            vector := rbd.AllocOp.get(storage_count),
            rd.SWOp.get(count, vector, 0, 'Set result count'),
            lhs := rd.AddIOp.get(op.rs1, 4, 'lhs storage'),
            rhs := rd.AddIOp.get(op.rs2, 4, 'lhs storage'),
            dest := rd.AddIOp.get(vector, 4, 'destination storage'),
            rbd.BufferAddOp.get(count, lhs, dest),
            rbd.BufferAddOp.get(count, rhs, dest),
        ], [vector.rd])


class LowerTensorMakeOp(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: tvd.TensorMakeOp,
                          rewriter: PatternRewriter):
        shape = op.rs1
        data = op.rs2

        tensor_storage_len_op = rd.LIOp.get(2, 'Tensor storage')
        tensor_op = rbd.AllocOp.get(tensor_storage_len_op)
        tensor_set_shape_op = rd.SWOp.get(shape, tensor_op, 0,
                                          'Set tensor shape')
        tensor_set_data_op = rd.SWOp.get(data, tensor_op, 4, 'Set tensor data')

        rewriter.replace_matched_op([
            tensor_storage_len_op,
            tensor_op,
            tensor_set_shape_op,
            rd.LWOp.get(tensor_op, 0),
            tensor_set_data_op,
        ], [tensor_op.rd])


class LowerTensorShapeOp(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: tvd.TensorShapeOp,
                          rewriter: PatternRewriter):
        rewriter.replace_matched_op(rd.LWOp.get(op.rs1, 0, 'Get tensor shape'))


class LowerTensorDataOp(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: tvd.TensorDataOp,
                          rewriter: PatternRewriter):
        rewriter.replace_matched_op(rd.LWOp.get(op.rs1, 4, 'Get tensor data'))
