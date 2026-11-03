"""User-facing error summaries keep technical detail in the event ledger."""
import re


def readable_error(error):
    text = str(error)
    if isinstance(error, TimeoutError):
        return text or "处理超时：等待资源或当前步骤超过了本次任务时限；已有产物保留在文件库"
    import httpx
    if isinstance(error, httpx.TimeoutException):
        return "所需服务未在时限内返回；本次请求已停止，实际用量以供应商返回为准"
    if isinstance(error, httpx.ConnectError):
        return "无法连接所需服务，请查看应用中的连接状态和本次任务阶段"
    if "Traceback" in text:
        text = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "代码执行失败")
    text = re.sub(r"(?i)(bearer\s+|sk-)[\w.\-]+", "[凭据已隐藏]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password)\s*[=:]\s*[^\s,;]+", r"\1=[已隐藏]", text)
    if "out of memory" in text.lower():
        return "本地内存或显存不足；已保存现有结果，请释放资源或调整图片服务"
    if "ConnectionError" in text or "ConnectError" in text:
        return "无法连接所需服务，请查看应用中的服务状态"
    return text[:400] or "执行未完成，详细原因已记录到任务中心"
