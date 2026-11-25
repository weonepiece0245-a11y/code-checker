# 示例：这段代码是【故意写错】的，用来演示运行时错误：空列表的平均值会“除以零”（ZeroDivisionError）。
def average(nums):
    total = 0
    for n in nums:
        total += n
    return total / len(nums)


print(average([1, 2, 3]))  # 正常：2.0
print(average([]))         # 出错：len([]) 是 0
