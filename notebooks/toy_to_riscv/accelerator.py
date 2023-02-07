# pyright: reportMissingTypeStubs=false

from riscemu.instructions import InstructionSet, Instruction
from riscemu.MMU import MMU
from riscemu.types import Int32

from functools import reduce


def tensor_description(shape: list[int], data: list[int]) -> str:
    if len(shape) == 1:
        return str(data)
    if len(shape):
        size = reduce(lambda acc, el: acc * el, shape[1:], 1)
        return f'[{", ".join(tensor_description(shape[1:], data[start:start+size]) for start in range(0, size * shape[0], size))}]'
    else:
        return '[]'


# Define a RISC-V ISA extension by subclassing InstructionSet
class ToyAccelerator(InstructionSet):
    # each method beginning with instruction_ will be available to the Emulator

    # add typed helpers
    @property
    def mu(self) -> MMU:
        'Memory Unit'
        return self.mmu  # type: ignore

    def set_reg(self, reg: str, value: int):
        self.regs.set(reg, Int32(value))  # type: ignore

    def get_reg(self, reg: str) -> int:
        return self.regs.get(reg).value  # type: ignore

    def ptr_read(self, ptr: int, /, offset: int = 0) -> int:
        byte_array = self.mu.read(ptr + offset * 4, 4)
        return int.from_bytes(byte_array, byteorder="little")

    def ptr_write(self, ptr: int, /, value: int, offset: int = 0):
        byte_array = bytearray(value.to_bytes(4, byteorder="little"))
        self.mu.write(ptr + offset * 4, 4, byte_array)

    def buffer_read(self, ptr: int, len: int, /, offset: int = 0) -> list[int]:
        return [
            self.ptr_read(ptr, offset)
            for offset in range(offset, offset + len)
        ]

    def buffer_write(self, ptr: int, /, data: list[int], offset: int = 0):
        for i, value in enumerate(data):
            self.ptr_write(ptr, value=value, offset=offset + i)

    def buffer_copy(self, /, source: int, destination: int, count: int):
        self.mu.write(destination, count * 4, self.mu.read(source, count * 4))

    # Vector helpers

    # A vector is represented as an array of ints, where the first int is the count:
    # [] -> [0]
    # [1] -> [1, 1]
    # [1, 2, 3] -> [3, 1, 2, 3]

    def vector_count(self, ptr: int) -> int:
        return self.ptr_read(ptr)

    def vector_data(self, ptr: int) -> list[int]:
        count = self.vector_count(ptr)
        return self.buffer_read(ptr, count, offset=1)

    def vector_end(self, ptr: int) -> int:
        return ptr + (1 + self.ptr_read(ptr)) * 4

    def vector_add(self, lhs: int, rhs: int):
        '''lhs += rhs'''
        data = [
            l + r
            for (l, r) in zip(self.vector_data(lhs), self.vector_data(rhs))
        ]
        self.buffer_write(lhs, data=data, offset=1)

    # Heap helpers

    # The heap pointer is the address of the start of the heap, and contains the count
    # of remaining allocated elements. Defaults to 0. This means that it can
    # be used as an append-only vector.

    def alloc(self, count: int, /, heap_ptr: int) -> int:
        result = self.vector_end(heap_ptr)
        heap_size = self.vector_count(heap_ptr)
        self.ptr_write(heap_ptr, value=heap_size + count)
        return result

    def vector_copy(self, ptr: int, /, heap_ptr: int) -> int:
        storage_len = self.vector_count(ptr) + 1
        new = self.alloc(storage_len, heap_ptr=heap_ptr)
        self.buffer_copy(source=ptr, destination=new, count=storage_len)
        return new

    # Tensor helpers

    # The tensor is represented as a vector, containing two pointers to vectors:
    # shape and data
    # [] -> [2, -> [0], -> [0]] (rank: 0, shape: [], count: 0, data: [])
    # [1, 2] -> [2, -> [1, 2], -> [2, 1, 2]] (rank: 1, shape: [2], count: 2, data: [1, 2])
    # [[1, 2, 3], [4, 5, 6]]
    #   -> [2, -> [2, 2, 3], -> [6, 1, 2, 3, 4, 5, 6]] (
    #       rank: 2,
    #       shape: [2, 3],
    #       count: 2,
    #       data: [1, 2, 3, 4, 5, 6]
    #   )

    # Where rank is the length of the shape subarray, and count is the length of data.

    def tensor_shape_array(self, ptr: int) -> int:
        return self.ptr_read(ptr, offset=0)

    def tensor_rank(self, ptr: int) -> int:
        return self.vector_count(self.tensor_shape_array(ptr))

    def tensor_shape(self, ptr: int) -> list[int]:
        return self.vector_data(self.tensor_shape_array(ptr))

    def tensor_data_array(self, ptr: int) -> int:
        return self.ptr_read(ptr, offset=1)

    def tensor_count(self, ptr: int):
        return self.vector_count(self.tensor_data_array(ptr))

    def tensor_data(self, ptr: int) -> list[int]:
        return self.vector_data(self.tensor_data_array(ptr))

    def tensor_copy(self, ptr: int, /, heap_ptr: int) -> int:
        # Shape is immutable, no need to copy
        shape = self.tensor_shape_array(ptr)
        old_data = self.tensor_data_array(ptr)
        new_data = self.vector_copy(old_data, heap_ptr=heap_ptr)
        return self.tensor_make(shape, new_data, heap_ptr=heap_ptr)

    def tensor_add(self, lhs: int, rhs: int):
        '''lhs += rhs'''
        self.vector_add(self.tensor_data_array(lhs),
                        self.tensor_data_array(rhs))

    def tensor_make(self, shape_ptr: int, data_ptr: int, /,
                    heap_ptr: int) -> int:
        result = self.alloc(3, heap_ptr=heap_ptr)
        self.ptr_write(result, value=2)
        self.ptr_write(result, value=shape_ptr, offset=1)
        self.ptr_write(result, value=data_ptr, offset=2)
        return result

    # Custom instructions

    def instruction_toy_tensor_print(self, ins: Instruction):
        """
        This instruction prints a formatted tensor
        [[1, 2, 3], [4, 5, 6]]
        """
        # get the input register
        t_ptr_reg = ins.get_reg(0)
        t_ptr = self.get_reg(t_ptr_reg)

        shape = self.tensor_shape(t_ptr)
        data = self.tensor_data(t_ptr)

        print(tensor_description(shape, data))

    def instruction_toy_buffer_add(self, ins: Instruction):

        c_reg, s_reg, d_reg = [ins.get_reg(i) for i in range(3)]

        count = self.get_reg(c_reg)

        s_ptr = self.get_reg(s_reg)
        d_ptr = self.get_reg(d_reg)

        s_data = self.buffer_read(s_ptr, count)
        d_data = self.buffer_read(d_ptr, count)

        self.buffer_write(d_ptr, data=[l + r for l, r in zip(s_data, d_data)])
