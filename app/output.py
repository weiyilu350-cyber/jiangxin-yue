"""T010 成品落地：把出完的图复制一份到主人配置的目录，外加「打开成品文件夹」。

红线：目标目录里的既有文件**一个都不许覆盖/移动/删除**——重名只能加后缀 `_1`、`_2`…
（用户指定的成品目录里有真作品，不能改动任何既有文件。）
原件永远在炉子 output 里，所以复制失败只记一句人话，绝不改任务的 done 状态。
"""
import os
import subprocess
from pathlib import Path

TMP_SUFFIX = ".huafang.tmp"


def unique_path(directory: Path, filename: str) -> Path:
    """在 directory 里给 filename 找一个不存在的名字：重名就 `_1`、`_2`… 只找，不建、不动既有文件。"""
    name = Path(filename).name          # 炉子给的名字按不可信处理：只取末段，防 `..\` 越界
    stem, suffix = Path(name).stem, Path(name).suffix
    cand = directory / name
    n = 0
    while cand.exists():
        n += 1
        cand = directory / f"{stem}_{n}{suffix}"
    return cand


def copy_finals(client, job, out_dir: str) -> None:
    """job 到 done 后把它的 final 图复制到 out_dir。空目录/只读记录/已复制过 → 什么都不做。"""
    if not out_dir or job.readonly or job.copied or job.status != "done" or not job.images:
        return
    job.copied = True                   # 先立旗：一次也不重试，绝不把轮询卡死在复制上
    directory = Path(out_dir)
    failed = []
    for img in job.images:
        name = Path(img.get("filename") or "").name
        if not name:
            continue
        tmp = None
        try:
            data = client.view_bytes(name, img.get("subfolder", ""))
            target = unique_path(directory, name)
            tmp = target.with_name(target.name + TMP_SUFFIX)
            tmp.write_bytes(data)
            if target.exists():         # 唯一名被抢（单用户本机几乎不可能）：宁可失败也不覆盖
                raise FileExistsError(target.name)
            os.replace(tmp, target)
            tmp = None
        except Exception:               # 任何异常都只记名字：done 状态与原件都不受影响
            failed.append(name)
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
    if failed:
        job.copy_error = ("已存炉子输出，复制到你的目录失败：" + "、".join(failed)
                          + "（原件没丢，在炉子的输出文件夹里）")


def reveal(directory) -> None:
    """打开文件夹：只把校验过的路径当**一个 argv 参数**交给 explorer，不拼命令行字符串。"""
    subprocess.Popen(["explorer", str(directory)])
