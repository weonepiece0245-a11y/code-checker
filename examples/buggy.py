# 示例：这段代码是【故意写错】的，用来演示代码检查助手能找出拼错的变量名（totl 应该是 total）。
# 空列表除以零的演示在 divide.py（两个错误放在一起时，程序在第一个错误就停了，第二个永远看不到）。
def average(nums):
    total = 0
    for n in nums:
        total += n
    return totl / len(nums)


print(average([1, 2, 3]))
