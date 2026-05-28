import difflib
import json
import os
import re
from datetime import datetime
from flask import Flask, request, render_template, send_from_directory, abort, jsonify
from device_connector import connect_to_device

app = Flask(__name__)


def generate_side_by_side_diff(lines_a, lines_b):
    """Generate a side-by-side diff with structured format."""
    matcher = difflib.SequenceMatcher(None, lines_a, lines_b)
    diff_lines = []
    
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for i in range(i2 - i1):
                line_a = lines_a[i1 + i].rstrip('\n')
                line_b = lines_b[j1 + i].rstrip('\n')
                diff_lines.append({
                    'type': 'equal',
                    'line_a': line_a,
                    'line_b': line_b
                })
        elif tag == 'delete':
            for i in range(i1, i2):
                line_a = lines_a[i].rstrip('\n')
                diff_lines.append({
                    'type': 'delete',
                    'line_a': line_a,
                    'line_b': ''
                })
        elif tag == 'insert':
            for j in range(j1, j2):
                line_b = lines_b[j].rstrip('\n')
                diff_lines.append({
                    'type': 'insert',
                    'line_a': '',
                    'line_b': line_b
                })
        elif tag == 'replace':
            # Handle replacements by pairing lines
            max_len = max(i2 - i1, j2 - j1)
            for k in range(max_len):
                line_a = lines_a[i1 + k].rstrip('\n') if i1 + k < i2 else ''
                line_b = lines_b[j1 + k].rstrip('\n') if j1 + k < j2 else ''
                if line_a and line_b:
                    diff_lines.append({
                        'type': 'replace',
                        'line_a': line_a,
                        'line_b': line_b
                    })
                elif line_a:
                    diff_lines.append({
                        'type': 'delete',
                        'line_a': line_a,
                        'line_b': ''
                    })
                else:
                    diff_lines.append({
                        'type': 'insert',
                        'line_a': '',
                        'line_b': line_b
                    })
    
    return diff_lines
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "configs")
os.makedirs(CONFIG_DIR, exist_ok=True)
META_FILE = os.path.join(CONFIG_DIR, "metadata.json")


def load_metadata():
    if not os.path.exists(META_FILE):
        return {}
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_metadata(metadata):
    try:
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_next_sequence_number():
    """Get the next sequence number based on existing files."""
    max_seq = 0
    for name in os.listdir(CONFIG_DIR):
        if not name.endswith(".txt"):
            continue
        name_without_ext = name[:-4]
        if "__" in name_without_ext:
            ts_and_seq = name_without_ext.split("__", 1)[0]
            parts = ts_and_seq.split("_")
            if len(parts) >= 1:
                try:
                    seq = int(parts[0])
                    max_seq = max(max_seq, seq)
                except ValueError:
                    pass
    return max_seq + 1


def list_saved_configs():
    metadata = load_metadata()
    records = []
    for name in sorted(os.listdir(CONFIG_DIR)):
        if not name.endswith(".txt"):
            continue
        path = os.path.join(CONFIG_DIR, name)
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        name_without_ext = name[:-4]
        
        seq = "unknown"
        timestamp_str = "unknown"
        host = "unknown"
        
        # File format: SEQ[_TICKET]_YYYYMMDD_HHMMSS__host.txt
        # Support both old format (SEQ_YYYYMMDD_HHMMSS__host.txt) and new format (SEQ_TICKET_YYYYMMDD_HHMMSS__host.txt)
        if "__" in name_without_ext:
            ts_and_seq, host = name_without_ext.split("__", 1)
            parts = ts_and_seq.split("_")
            if len(parts) >= 2:
                try:
                    seq = int(parts[0])
                    # Find the timestamp by looking for YYYYMMDD_HHMMSS pattern from the end
                    # The last two parts should form the timestamp (YYYYMMDD and HHMMSS)
                    if len(parts) >= 3:
                        potential_ts = "_".join(parts[-2:])
                        try:
                            datetime.strptime(potential_ts, "%Y%m%d_%H%M%S")
                            timestamp_str = potential_ts
                        except ValueError:
                            # If last two don't form a valid timestamp, fallback to original logic
                            timestamp_str = "_".join(parts[1:])
                    else:
                        timestamp_str = parts[1]
                except (ValueError, IndexError):
                    pass
        
        try:
            dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
            timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            timestamp = mtime.strftime("%Y-%m-%d %H:%M:%S")
        
        host_display = host.replace("_", ".")
        meta = metadata.get(name, {})
        records.append({
            "seq": seq,
            "filename": name,
            "host": host_display,
            "timestamp": timestamp,
            "comment": meta.get("comment", ""),
            "ticket": meta.get("ticket", ""),
        })
    
    # Sort by sequence number descending so newest records appear first
    records.sort(key=lambda x: (x["seq"] if isinstance(x["seq"], int) else -1), reverse=True)
    return records


@app.route("/")
def index():
    records = list_saved_configs()
    return render_template(
        "index.html",
        records=records,
        compare_result=None,
        changes_count=0,
        compare_a=None,
        compare_b=None,
    )


@app.route("/connect", methods=["POST"])
def connect():
    host_input = request.form["host"].strip()
    username = request.form["username"]
    password = request.form["password"]
    
    # 支持多个host，用逗号或换行分隔
    hosts = [h.strip() for h in re.split(r'[,\n]', host_input) if h.strip()]
    
    if not hosts:
        records = list_saved_configs()
        return render_template(
            "index.html",
            records=records,
            error="请输入至少一个Host地址。",
            compare_result=None,
            changes_count=0,
            compare_a=None,
            compare_b=None,
        )

    comment_choice = request.form.get("comment", "none")
    other_text = request.form.get("comment_text", "").strip()
    ticket = request.form.get("ticket", "").strip()
    if comment_choice == "before change":
        comment = "before change"
    elif comment_choice == "after change":
        comment = "after change"
    elif comment_choice == "other":
        comment = other_text
    else:
        comment = ""
    
    success_list = []
    error_list = []
    
    for host in hosts:
        try:
            output = connect_to_device(host, username, password)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_host = re.sub(r"[^a-zA-Z0-9_-]", "_", host)
            seq = get_next_sequence_number()
            
            # Generate filename with optional ticket ID
            # Format: SEQ[_TICKET]_YYYYMMDD_HHMMSS__host.txt
            if ticket:
                filename = f"{seq}_{ticket}_{timestamp}__{safe_host}.txt"
            else:
                filename = f"{seq}_{timestamp}__{safe_host}.txt"
            
            file_path = os.path.join(CONFIG_DIR, filename)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(output)

            metadata = load_metadata()
            metadata[filename] = {
                "comment": comment,
                "ticket": ticket,
            }
            save_metadata(metadata)
            
            success_list.append({
                "host": host,
                "filename": filename,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        except Exception as e:
            error_list.append({
                "host": host,
                "error": str(e)
            })

    records = list_saved_configs()
    
    if error_list and not success_list:
        # 全部失败
        error_msg = "连接失败: " + "; ".join([f"{e['host']}: {e['error']}" for e in error_list])
        return render_template(
            "index.html",
            records=records,
            error=error_msg,
            compare_result=None,
            changes_count=0,
            compare_a=None,
            compare_b=None,
        )
    else:
        # 部分成功或全部成功
        return render_template(
            "index.html",
            records=records,
            success=True,
            success_list=success_list,
            error_list=error_list,
            compare_result=None,
            changes_count=0,
            compare_a=None,
            compare_b=None,
        )


@app.route("/compare", methods=["POST"])
def compare():
    file_a = request.form.get("file_a")
    file_b = request.form.get("file_b")
    records = list_saved_configs()

    if not file_a or not file_b:
        return render_template(
            "index.html",
            records=records,
            error="请选择两个要比较的配置文件。",
        )
    if file_a == file_b:
        return render_template(
            "index.html",
            records=records,
            error="请选择两个不同的配置文件进行比较。",
        )
    for filename in (file_a, file_b):
        if ".." in filename or filename.startswith("/") or not filename.endswith(".txt"):
            abort(400)
        if not os.path.exists(os.path.join(CONFIG_DIR, filename)):
            abort(404)

    path_a = os.path.join(CONFIG_DIR, file_a)
    path_b = os.path.join(CONFIG_DIR, file_b)
    with open(path_a, "r", encoding="utf-8", errors="ignore") as fa:
        lines_a = fa.readlines()
    with open(path_b, "r", encoding="utf-8", errors="ignore") as fb:
        lines_b = fb.readlines()

    # Generate structured diff
    diff_lines = generate_side_by_side_diff(lines_a, lines_b)
    
    # Count changes
    changes_count = sum(1 for line in diff_lines if line['type'] != 'equal')

    return render_template(
        "index.html",
        records=records,
        compare_result=json.dumps(diff_lines, ensure_ascii=False),
        changes_count=changes_count,
        compare_a=file_a,
        compare_b=file_b,
    )


@app.route("/delete/<path:filename>", methods=["POST"])
def delete_config(filename):
    if ".." in filename or filename.startswith("/") or not filename.endswith(".txt"):
        abort(400)
    file_path = os.path.join(CONFIG_DIR, filename)
    if not os.path.exists(file_path):
        abort(404)
    try:
        os.remove(file_path)
    except OSError:
        pass
    metadata = load_metadata()
    if filename in metadata:
        metadata.pop(filename, None)
        save_metadata(metadata)
    records = list_saved_configs()
    return render_template(
        "index.html",
        records=records,
        compare_result=None,
        changes_count=0,
        compare_a=None,
        compare_b=None,
    )


@app.route("/configs/<path:filename>")
def view_config(filename):
    if ".." in filename or filename.startswith("/"):
        abort(400)
    return send_from_directory(CONFIG_DIR, filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)



