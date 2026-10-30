import numpy as np

# 1. Create a 1D array and a 2D array (matrix)
array_1d = np.array([1, 2, 3, 4, 5])
matrix_2d = np.array([[1, 2, 3], [4, 5, 6]])
matrix_3d = np.array([[1, 2, 3], [4, 5, 6],[7, 8, 9]])

print("1D Array:\n", array_1d)
print("\n2D Matrix:\n", matrix_2d)

# 2. Basic array attributes
print("\nMatrix shape:", matrix_2d.shape)  # (rows, columns)  # I've made this change.
print("Matrix data type:", matrix_2d.dtype)

# 3. Mathematical operations (vectorized)
# Adding 10 to every element in the 1D array
added_array = array_1d + 10
print("\n1D Array + 10:", added_array)

# Element-wise multiplication
squared_array = array_1d ** 2
print("1D Array squared:", squared_array)

# 4. Statistical aggregations
print("\nSum of all elements in matrix:", np.sum(matrix_2d))
print("Mean of matrix columns (axis=0):", np.mean(matrix_2d, axis=0))
print("Max value in matrix:", np.max(matrix_2d))

# 5. Reshaping arrays
# Create a sequence of numbers from 0 to 11 and reshape into a 3x4 matrix
sequence = np.arange(12).reshape(3, 4)
print("\nReshaped 3x4 Matrix:\n", sequence)