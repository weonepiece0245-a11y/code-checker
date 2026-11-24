# 代码检查助手（codecheck）

检查代码 → 在受限环境里实际运行 → 用 AI 讲解错误、给修改建议和修好后的代码。

目前支持的编程语言：**Python**、**JavaScript**。AI 默认用**简体中文**讲解（可以改成繁体中文或英文）。

## Windows 快速开始

1. 安装 [Python](https://www.python.org/downloads/)（3.10 或更高版本），安装时**勾选 "Add python.exe to PATH"**。
2. 检查 JavaScript 代码还需要安装 [Node.js](https://nodejs.org/)（只检查 Python 可以不装）。
3. 把解压出来的文件夹放到一个固定的位置（比如 `D:\code-checker`），然后双击 **`启动网页版.bat`**。第一次运行会自动安装组件，然后自动打开浏览器。
4. 运行一次后，桌面上会出现 **“代码检查助手”** 图标，以后双击它就能打开。
5. 用完后关掉那个黑色命令行窗口即可停止。

## 自动更新

在 `update_source.txt` 里写上 GitHub 仓库（格式：`用户名/仓库名`）。之后每次启动都会检查有没有新版本，有的话自动下载并替换。没联网或文件为空时直接跳过。

## 手动安装

需要 Python 3.10 或更高版本；检查 JavaScript 还需要安装 Node.js。

```bash
pip install -r requirements.txt
```

## AI 怎么接（任选一种）

| 方式 | 怎么设置 | 费用 |
|---|---|---|
| **Claude Code 订阅**（推荐自己用） | 安装 Claude Code，并在终端运行一次 `claude` 登录 | 算在你的 Claude 订阅额度里，不需要 API key |
| **API key** | 设置环境变量 `ANTHROPIC_API_KEY=你的key` | 按调用量付费 |
| 不用 AI | 加参数 `--ai none` | 免费，只有静态检查和运行结果 |

默认是 `auto`：装了 Claude Code 就用订阅额度，没有才用 API key。

## 用法一：命令行

```bash
python -m codecheck examples/buggy.py
python -m codecheck examples/buggy.js --ai none        # 不用 AI，速度快
python -m codecheck 我的代码.py --input "5\n3"          # 给程序喂输入
python -m codecheck 我的代码.py --explain-lang English  # 用英文讲解
python -m codecheck 我的代码.py --json                  # 输出 JSON，给其他程序调用
```

## 用法二：网页

```bash
python -m codecheck.web.server
```

然后用浏览器打开 http://127.0.0.1:8765 ，粘贴代码，点“检查”即可。页面里有几个带 bug 的示例可以直接试。

## 运行安全吗？

- **本机（受限）模式**（默认）：限制运行时间（默认 5 秒）、内存（256MB）和写入文件的大小。运行**自己写的代码**用这个就够了。
- **Docker 隔离模式**（`--sandbox docker`）：在容器里运行，不能联网、文件只读。运行**来路不明的代码**时请用这个，需要先安装并启动 Docker。
- 网页服务只监听本机（127.0.0.1），别人访问不到。

## 项目结构

```
codecheck/
  languages.py     支持的编程语言
  static_check.py  静态检查（不运行就能发现的错误）
  runner.py        受限运行 / Docker 运行
  ai.py            AI 分析（API key 或 Claude Code）
  core.py          把上面几步串起来
  cli.py           命令行
  web/             网页版
examples/          带 bug 的示例代码
```

## 后续计划

- [ ] VS Code 插件：保存文件时自动检查（调用 `python -m codecheck 文件 --json`）
- [ ] GitHub 机器人：有人提交 PR 时自动检查并留言
- [ ] 一键应用 AI 的修改
- [ ] 让 AI 自动生成测试用例并实际运行
- [ ] 支持更多编程语言（Java、C/C++、Go……）
