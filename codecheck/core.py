"""把 静态检查 → 运行 → AI 分析 串起来。CLI 和网页都调用这里。"""
import os
import tempfile

from . import ai, runner, static_check
from .languages import LANGUAGES, code_suffix, detect


def check_code(code: str, filename: str | None = None, language: str | None = None, stdin: str = "",
               run: bool = True, timeout: int = 5, memory_mb: int = 256, sandbox: str = "local",
               ai_backend: str = "auto", explain_lang: str = "简体中文", workdir: str | None = None,
               source_path: str | None = None) -> dict:
    """source_path：代码对应的真实文件。给了它，运行时就像在原位置一样（能 import 同目录的模块）。"""
    if source_path:
        source_path = os.path.abspath(source_path)
        filename = filename or source_path
        workdir = workdir or os.path.dirname(source_path)
    lang = LANGUAGES[language] if language else detect(filename, code)
    with tempfile.TemporaryDirectory(prefix="codecheck_") as tmp:
        if sandbox == "docker":
            os.chmod(tmp, 0o755)  # 只有 Docker 里的程序需要读；本机运行时保持只有自己能读
        path = os.path.join(tmp, "main" + code_suffix(filename, lang))
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        issues = [i.to_dict() for i in static_check.check(lang.key, path)]
        result = None
        run_error = ""
        if run:
            try:
                result = runner.run(lang, path, stdin, timeout, memory_mb, sandbox, workdir=workdir,
                                    source_path=source_path).to_dict()
            except RuntimeError as e:
                run_error = str(e)
        # 报错信息里的临时路径换成文件名，读起来更清楚
        shown = filename or os.path.basename(path)
        for i in issues:
            i["message"] = i["message"].replace(path, shown)
        if result:
            result["stderr"] = result["stderr"].replace(path, shown)

    report = {
        "language": lang.name,
        "issues": issues,
        "run": result,
        "run_error": run_error,
        "ai_backend": "none",
        "ai": "",
        "ai_error": "",
    }
    if ai_backend != "none":
        try:
            report["ai_backend"], report["ai"] = ai.analyze(
                lang.name, code, issues, result, stdin, ai_backend, explain_lang)
        except Exception as e:  # AI 出错不影响其他结果
            report["ai_error"] = str(e)
    errors = sum(1 for i in issues if i["severity"] == "error")
    # 没能运行（比如没装 Node.js）不能算“通过”
    report["passed"] = errors == 0 and not run_error and (result is None or result["ok"])
    return report
