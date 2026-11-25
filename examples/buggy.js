// 示例：这段代码是【故意写错】的，用来演示代码检查助手（循环越界、缺少 age 字段）。
const users = [{ name: "小明", age: 18 }, { name: "小红" }];

function oldest(list) {
  let best = list[0];
  for (let i = 0; i <= list.length; i++) {
    if (list[i].age > best.age) best = list[i];
  }
  return best.name;
}

console.log(oldest(users));
