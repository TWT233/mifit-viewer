#!/usr/bin/env python3
"""Mi Fitness 体脂秤数据可视化 Web App.

读取小米运动健康导出的体脂秤 CSV,解析后以 JSON 暴露给前端折线图展示。
使用标准库 http.server,无需第三方依赖。

用法: python3 app.py [--data-dir DIR] [--port 8000]
"""
import argparse
import csv
import json
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 北京时区，用于把 unix 时间戳转成可读日期
CN_TZ = timezone(timedelta(hours=8))

# 数据统一放在项目下的 data/ 目录,递归扫描其中的体脂秤 CSV
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 字段定义: key -> (中文名, 单位, 分组, 是否仅完整测量才有)
# 按量纲分类,同量纲指标归为一组,便于横向比较;单位为空表示无量纲
METRICS = [
    # 质量 (kg)
    ("weight",     "体重",       "kg",   "质量 (kg)",     False),
    ("swt",        "标准体重",   "kg",   "质量 (kg)",     True),
    ("bfm",        "脂肪量",     "kg",   "质量 (kg)",     True),
    ("ffm",        "去脂体重",   "kg",   "质量 (kg)",     True),
    ("slm",        "瘦体重",     "kg",   "质量 (kg)",     True),
    ("smm",        "骨骼肌量",   "kg",   "质量 (kg)",     True),
    ("bwm",        "体水分量",   "kg",   "质量 (kg)",     True),
    ("pm",         "蛋白量",     "kg",   "质量 (kg)",     True),
    ("bmc",        "骨盐量",     "kg",   "质量 (kg)",     True),
    ("mc",         "肌肉控制",   "kg",   "质量 (kg)",     True),
    ("wc",         "体重控制",   "kg",   "质量 (kg)",     True),
    ("fc",         "脂肪控制",   "kg",   "质量 (kg)",     True),
    # 比率 (%)
    ("bfp",        "体脂率",     "%",    "比率 (%)",      True),
    ("bwp",        "体水分率",   "%",    "比率 (%)",      True),
    ("pp",         "蛋白率",     "%",    "比率 (%)",      True),
    ("bmcp",       "骨盐量率",   "%",    "比率 (%)",      True),
    # 指数与等级 (无量纲)
    ("bmi",        "BMI",        "",     "指数与等级",    True),
    ("vfl",        "内脏脂肪等级", "",    "指数与等级",    True),
    ("whr",        "腰臀比",     "",     "指数与等级",    True),
    ("sbc",        "身体得分",   "",     "指数与等级",    True),
    # 其他生理参数
    ("heartRate",  "心率",       "bpm",  "其他",          True),
    ("bmr",        "基础代谢率", "kcal", "其他",          True),
    ("ma",         "身体年龄",   "岁",   "其他",          True),
]


def parse_csv(csv_path):
    """读取 CSV，返回按时间排序的测量记录列表。

    每条记录含 ISO 日期、用户标识和各指标数值。weight 对所有有效称重可用，
    其余体脂指标仅在完整测量(bfp>0)时有意义。account_id/duid/user_type 仅供
    build_payload 聚合用户列表,之后剥离。
    """
    records = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts = int(r["time"])
            except (ValueError, KeyError):
                continue
            if (_to_float(r.get("weight")) or 0) <= 0:
                continue  # 跳过无效称重
            complete = (_to_float(r.get("bfp")) or 0) > 0
            rec = {
                "time": ts,  # 仅用于排序,输出时剥离
                "date": datetime.fromtimestamp(ts, CN_TZ).strftime("%Y-%m-%d %H:%M"),
                "user_key": "{}_{}".format(r.get("account_id", ""), r.get("duid", "")),
                "account_id": r.get("account_id", ""),
                "duid": r.get("duid", ""),
                "user_type": r.get("userType", ""),
            }
            for key, _, _, _, complete_only in METRICS:
                val = _to_float(r.get(key))
                rec[key] = None if (complete_only and not complete) else val
            records.append(rec)
    records.sort(key=lambda x: x["time"])
    return records


def _to_float(v):
    if v is None or v == "" or v == "null":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def build_payload(records):
    """组装前端所需的 JSON 结构。"""
    groups = {}
    for key, name, unit, group, complete_only in METRICS:
        groups.setdefault(group, []).append({
            "key": key, "name": name, "unit": unit, "complete_only": complete_only,
        })
    # 按档案汇总用户列表
    users = {}
    for r in records:
        u = users.setdefault(r["user_key"], {
            "user_key": r["user_key"], "duid": r["duid"], "user_type": r["user_type"],
            "count": 0, "weight_sum": 0.0, "weight_n": 0,
        })
        u["count"] += 1
        if r.get("weight"):
            u["weight_sum"] += r["weight"]; u["weight_n"] += 1
    user_list = []
    for u in users.values():
        mean = u["weight_sum"] / u["weight_n"] if u["weight_n"] else 0
        role = "主人" if u["user_type"] == "1" else "成员"
        u["label"] = "成员 (槽位{}, {}, 均重{:.1f}kg, {}条)".format(u["duid"], role, mean, u["count"])
        u["weight_mean"] = round(mean, 1)
        user_list.append(u)
    user_list.sort(key=lambda x: x["duid"])
    # 剥离仅用于排序/聚合的字段,减小 JSON
    strip = {"time", "account_id", "duid", "user_type"}
    slim = [{k: v for k, v in r.items() if k not in strip} for r in records]
    return {
        "generated_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "groups": groups,
        "users": user_list,
        "records": slim,
    }


def is_scale_csv(path):
    """通过表头判断是否为体脂秤 CSV(含 weight/bfp/time 等关键列)。"""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
    except Exception:
        return False
    return all(c in header for c in ("weight", "bfp", "time", "bmi"))


def scan_csv_files(root):
    """递归扫描 root 下所有看起来是体脂秤数据的 CSV(按表头判断,不依赖文件名)。

    跳过隐藏目录和 archive 目录(归档区不参与扫描)。
    """
    files = []
    for dirpath, _, filenames in os.walk(root):
        base = os.path.basename(dirpath)
        if base.startswith(".") or base == "archive":
            continue
        for fn in filenames:
            if not fn.endswith(".csv"):
                continue
            full = os.path.join(dirpath, fn)
            if is_scale_csv(full):
                rel = os.path.relpath(full, root)
                files.append({"id": rel, "name": fn, "path": full})
    files.sort(key=lambda x: x["id"])
    return files


def _unique_move(src, dst_dir, name):
    """把 src 移到 dst_dir/name,同名时加 _1/_2 后缀,返回目标相对 dst_dir 的名字。"""
    stem, ext = os.path.splitext(name)
    dst = os.path.join(dst_dir, name)
    i = 1
    while os.path.exists(dst):
        dst = os.path.join(dst_dir, f"{stem}_{i}{ext}")
        i += 1
    shutil.move(src, dst)
    return os.path.basename(dst)


def organize_data_dir(root):
    """启动时整理 data 目录:
    1. 把体脂秤 CSV 从子目录提到 root 直接下(同名加序号);
    2. 其余文件/目录(非体脂秤 CSV)移到 root/archive/。
    返回 (提到直接下的 CSV 相对路径列表, 归档项数)。
    """
    if not os.path.isdir(root):
        return [], 0
    archive = os.path.join(root, "archive")
    os.makedirs(archive, exist_ok=True)

    # 1. 提体脂秤 CSV 到直接下
    moved_csv = []
    for f in scan_csv_files(root):
        if os.path.abspath(os.path.dirname(f["path"])) == os.path.abspath(root):
            continue  # 已在直接下
        moved_csv.append(_unique_move(f["path"], root, f["name"]))

    # 2. 归档直接下所有非体脂秤 CSV 的项(跳过 archive 自身)
    archived = 0
    for name in os.listdir(root):
        if name == "archive":
            continue
        full = os.path.join(root, name)
        if os.path.isfile(full) and name.endswith(".csv") and is_scale_csv(full):
            continue  # 体脂秤 CSV 保留在直接下
        _unique_move(full, archive, name)
        archived += 1
    return moved_csv, archived


class Handler(BaseHTTPRequestHandler):
    data_dir = os.path.dirname(os.path.abspath(__file__))
    files = []
    _payloads = {}  # file_id -> payload 缓存

    @classmethod
    def scan(cls):
        cls.files = scan_csv_files(cls.data_dir)
        cls._payloads = {}

    def log_message(self, *args):  # 静默默认日志
        pass

    @classmethod
    def get_payload(cls, file_id):
        if not cls.files:
            return None
        if file_id is None:
            file_id = cls.files[0]["id"]
        f = next((x for x in cls.files if x["id"] == file_id), None)
        if not f:
            return None
        if file_id not in cls._payloads:
            records = parse_csv(f["path"])
            payload = build_payload(records)
            payload["file_id"] = file_id
            payload["file_name"] = f["name"]
            cls._payloads[file_id] = payload
        return cls._payloads[file_id]

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/api/files":
            body = json.dumps({
                "data_dir": self.data_dir,
                "files": [{"id": f["id"], "name": f["name"]} for f in self.files],
            }, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif path == "/api/data":
            file_id = qs.get("file", [None])[0]
            payload = self.get_payload(file_id)
            if payload is None:
                self._send(404, "application/json; charset=utf-8",
                           b'{"error":"file not found"}')
                return
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        else:
            self.send_error(404)

    def _serve_file(self, name, ctype):
        fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        try:
            with open(fpath, "rb") as f:
                body = f.read()
            self._send(200, ctype, body)
        except FileNotFoundError:
            self.send_error(404, f"{name} not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description="Mi Fitness 体脂秤数据可视化")
    ap.add_argument("--data-dir", default=DATA_DIR,
                    help="数据目录,递归扫描其中的体脂秤 CSV")
    ap.add_argument("--port", type=int, default=8000, help="监听端口")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址")
    args = ap.parse_args()

    Handler.data_dir = args.data_dir
    # 启动时整理:体脂秤 CSV 提到 data/ 直接下,其余归档到 data/archive/
    moved_csv, archived = organize_data_dir(args.data_dir)
    if moved_csv:
        print(f"整理: {len(moved_csv)} 个体脂秤 CSV 已移至 data/ 直接下:")
        for m in moved_csv:
            print(f"  ← {m}")
    if archived:
        print(f"整理: {archived} 个非体脂秤文件/目录已归档到 data/archive/")
    Handler.scan()
    print(f"数据目录: {args.data_dir}")
    print(f"扫描到 {len(Handler.files)} 个体脂秤 CSV:")
    for f in Handler.files:
        print(f"  - {f['id']}")
    if not Handler.files:
        print("  (未找到体脂秤 CSV,请把 *scale_record.csv 放入 data/ 后重启)")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"服务已启动: {url}  (Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.shutdown()


if __name__ == "__main__":
    main()
