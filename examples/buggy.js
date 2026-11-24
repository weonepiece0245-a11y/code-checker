const users = [{ name: "小明", age: 18 }, { name: "小红" }];

function oldest(list) {
  let best = list[0];
  for (let i = 0; i <= list.length; i++) {
    if (list[i].age > best.age) best = list[i];
  }
  return best.name;
}

console.log(oldest(users));
