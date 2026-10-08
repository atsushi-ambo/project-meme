"""ローカルの解析プロセスを時間と出力量で制限する。入力は標準入力へ渡さない。"""
import os
import selectors
import subprocess
import time


class OutputLimitExceeded(subprocess.SubprocessError):
    """解析結果が出力上限を超えた。生データは例外やログへ入れない。"""


def run_bounded(command, *, timeout=15, max_output=131072):
    """固定の引数配列を実行し、上限内の標準出力だけをbytesで返す。"""
    if timeout <= 0 or max_output < 0:
        raise ValueError('invalid_process_limit')
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise subprocess.TimeoutExpired(command, timeout)
                    # 超過を検知する1バイトだけを余分に読み、出力を無制限に蓄積しない。
                    chunk = os.read(process.stdout.fileno(), min(65536, max_output - len(output) + 1))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > max_output:
                        raise OutputLimitExceeded('process_output_limit')
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
            if returncode:
                raise subprocess.CalledProcessError(returncode, command)
            return bytes(output)
        except BaseException:
            # timeout・容量超過・キャンセルでも子を残さず回収する。
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
