# 示例：这段代码是【故意写错】的，用来演示代码检查助手能找出哪些问题（拼错变量名、空列表除以零）。
def average(nums):
    total = 0
    for n in nums:
        total += n
    return totl / len(nums)

print(average([1, 2, 3]))
print(average([]))
