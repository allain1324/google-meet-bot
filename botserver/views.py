# views.py
from django.shortcuts import render
from django.http import JsonResponse, HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt
from pathlib import Path
from uuid import uuid4
from django.http import JsonResponse, FileResponse, Http404
import json, re, subprocess, shlex
import os


RECORD_DIR = Path("/var/app/recordings")
MEET_RE = re.compile(r"^https?://meet\.google\.com/[a-z0-9-]+(\?.*)?$", re.I)

def index(request):
    if request.method == "POST":
        link = request.POST.get('meetlink','').strip()
        if link:
            _start_bot(link)
    return render(request,'index.html',context=None)

@csrf_exempt
def api_submit_url(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    if request.content_type and "application/json" in (request.content_type or ""):
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        link = (data.get("meetlink") or data.get("link") or "").strip()
        message_id = str(data.get("message_id", "")).strip() or None
        headless = str(data.get("headless","")).lower() in ("1","true","yes")
    else:
        link = request.POST.get("meetlink","").strip()
        message_id = request.POST.get("message_id","").strip() or None
        headless = str(request.POST.get("headless","")).lower() in ("1","true","yes")

    if not link or not MEET_RE.match(link):
        return JsonResponse({"error": "Invalid Google Meet link"}, status=400)

    # 1) tạo tên file trước ở view
    filename = f"rec-{uuid4().hex}.mkv"   # hoặc .mp4 nếu bạn đổi container
    os.makedirs(RECORD_DIR, exist_ok=True)

    # 2) gọi meetbot và truyền REC_OUT qua ENV
    env = os.environ.copy()
    env["REC_OUT"] = filename
    env["MESSAGE_ID"] = message_id
    # (tuỳ chọn) bạn cũng có thể set REC_DIR/REC_WIDTH/REC_HEIGHT ở đây

    subprocess.run(["chmod","+x","./botserver/meetbot.py"], check=False)
    args = ['python3','./botserver/meetbot.py', link]
    if headless:
        args.append('--headless')

    proc = subprocess.Popen(args, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    # 3) trả về ngay cho client
    return JsonResponse({
        "status": "queued",
        "pid": proc.pid,
        "meetlink": link,
        "filename": filename,
        "message_id": message_id,
        "file_url": f"/api/recordings/{filename}"
    }, status=202)
    
def api_get_recording(request, fname: str):
    safe = os.path.basename(fname)             # chống path traversal
    path = RECORD_DIR / safe
    if not (path.exists() and path.is_file()):
        raise Http404("Not found")
    return FileResponse(open(path, "rb"), as_attachment=True, filename=safe)

@csrf_exempt
def api_delete_record(request, fname: str):
    if request.method != "DELETE":
        return HttpResponseNotAllowed(["DELETE"])

    safe = os.path.basename(fname)  # chống path traversal
    path = RECORD_DIR / safe

    if not path.exists() or not path.is_file():
        return JsonResponse({"error": "File not found"}, status=404)

    try:
        os.remove(path)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({
        "status": "deleted",
        "filename": safe
    }, status=200)

def _start_bot(link: str):
    from uuid import uuid4
    filename = f"rec-{uuid4().hex}.mkv"
    env = os.environ.copy()
    env["REC_OUT"] = filename
    subprocess.run(["chmod","+x","./botserver/meetbot.py"], check=False)
    subprocess.Popen(['python3','./botserver/meetbot.py', link],
                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return filename