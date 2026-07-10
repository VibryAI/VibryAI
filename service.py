"""
Vibry AI Core — Windows 服务包装器

安装:   python service.py install
启动:   python service.py start        (或 net start VibryAICore)
停止:   python service.py stop         (或 net stop VibryAICore)
重启:   python service.py restart
卸载:   python service.py remove
调试:   python service.py debug        (前台运行，看日志)
"""

import sys
import os
import logging
import servicemanager
import win32event
import win32service
import win32serviceutil

# 确保项目根目录在 sys.path
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

SERVICE_NAME = "VibryAICore"
DISPLAY_NAME = "Vibry AI Core Server"
DESCRIPTION = "数字前额叶记忆代理 + AI 分析后端 (FastAPI)"


class VibryService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._server = None

    def SvcDoRun(self):
        """服务启动入口"""
        self.ReportServiceStatus(win32service.SERVICE_START_PENDING)
        try:
            self._run()
        except Exception as e:
            servicemanager.LogErrorMsg(f"Service crashed: {e}")
            self.ReportServiceStatus(win32service.SERVICE_STOPPED)

    def _run(self):
        import uvicorn
        import threading
        from app.config import config

        # 重定向 stdout/stderr 到日志文件（Windows 服务无控制台）
        log_file = os.path.join(PROJECT_DIR, "server_output.log")
        sys.stdout = open(log_file, "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

        # 在后台线程启动 uvicorn
        config_dict = {
            "app": "main:app",
            "host": config.server.host,
            "port": config.server.port,
            "log_level": config.server.log_level.lower(),
            "reload": False,
        }

        def run_server():
            uvicorn.run(**config_dict)

        t = threading.Thread(target=run_server, daemon=True)
        t.start()

        # 等服务器就绪后回报 SCM
        import time
        time.sleep(2)
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, f"http://{config.server.host}:{config.server.port}"),
        )

        # 等待停止信号
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)

    def SvcStop(self):
        """服务停止"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, ""),
        )
        self.ReportServiceStatus(win32service.SERVICE_STOPPED)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("")  # 触发 win32serviceutil 的 help

    win32serviceutil.HandleCommandLine(
        VibryService,
        serviceClassString=SERVICE_NAME,
        argv=sys.argv if len(sys.argv) > 1 else None,
    )
