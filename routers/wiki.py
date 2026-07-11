"""Vibry AI Core — Wiki RAG API endpoints"""
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from services.wiki import (
    init_wiki, is_wiki_initialized, get_wiki_status,
    ingest, save_raw, list_raw, get_raw,
    query as wiki_query, archive_answer,
    list_articles, get_article, delete_article,
    rebuild_index, lint as wiki_lint,
)

router = APIRouter()

@router.get("/api/wiki/status")
async def api_wiki_status(): return JSONResponse(get_wiki_status())

@router.post("/api/wiki/init")
async def api_wiki_init(): return JSONResponse(init_wiki())

@router.post("/api/wiki/ingest")
async def api_wiki_ingest(request: Request):
    data = await request.json()
    content = data.get("content",""); title = data.get("title",""); topic = data.get("topic","general")
    source_url = data.get("source_url",""); published_date = data.get("published_date",""); model = data.get("model") or None
    if not content.strip(): raise HTTPException(status_code=400, detail="content required")
    if not title.strip(): raise HTTPException(status_code=400, detail="title required")
    result = ingest(content=content, title=title, topic=topic, source_url=source_url, published_date=published_date, model=model)
    if not result.get("ok"): return JSONResponse(result, status_code=500)
    return JSONResponse(result)

@router.post("/api/wiki/ingest/file")
async def api_wiki_ingest_file(request: Request, file: UploadFile = File(...), topic: str = Form("general"), source_url: str = Form(""), model: str = Form("")):
    content_bytes = await file.read()
    try: content = content_bytes.decode("utf-8")
    except: content = content_bytes.decode("gbk", errors="replace")
    title = file.filename or "uploaded"; model_arg = model.strip() if model else None
    result = ingest(content=content, title=title, topic=topic.strip() or "general", source_url=source_url.strip(), model=model_arg)
    return JSONResponse(result)

@router.post("/api/wiki/ingest/batch")
async def api_wiki_ingest_batch(request: Request):
    data = await request.json(); files = data.get("files",[]); model = data.get("model") or None
    if not files: raise HTTPException(status_code=400, detail="files required")
    results = []
    for f in files:
        if not f.get("content","").strip(): results.append({"title":f.get("title","untitled"),"ok":False,"error":"empty content"}); continue
        r = ingest(content=f["content"], title=f.get("title","untitled"), topic=f.get("topic","general"), source_url=f.get("source_url",""), published_date=f.get("published_date",""), model=model)
        results.append(r)
    return JSONResponse({"batch_count":len(files),"results":results})

@router.post("/api/wiki/query")
async def api_wiki_query(request: Request):
    data = await request.json(); q = data.get("query","")
    if not q.strip(): raise HTTPException(status_code=400, detail="query required")
    if not is_wiki_initialized(): return JSONResponse({"query":q,"count":0,"results":[],"answer":"","hint":"Wiki not initialized"})
    result = wiki_query(q, top_k=data.get("top_k",5), use_embedding=data.get("use_embedding",False), generate_answer=data.get("generate_answer",False), model=data.get("model") or None)
    return JSONResponse(result)

@router.post("/api/wiki/archive")
async def api_wiki_archive(request: Request):
    data = await request.json()
    answer = data.get("answer",""); query_text = data.get("query",""); topic = data.get("topic","general")
    if not answer.strip(): raise HTTPException(status_code=400, detail="answer required")
    return JSONResponse(archive_answer(query_text, answer, topic))

@router.post("/api/wiki/lint")
async def api_wiki_lint(request: Request):
    if not is_wiki_initialized(): raise HTTPException(status_code=400, detail="Wiki not initialized")
    data = await request.json()
    result = wiki_lint(auto_fix=data.get("auto_fix",True), heuristic=data.get("heuristic",False), model=data.get("model") or None)
    return JSONResponse(result)

@router.get("/api/wiki/pages")
async def api_wiki_list_pages(topic: str = None):
    articles = list_articles(topic=topic)
    return JSONResponse({"count":len(articles),"articles":articles})

@router.get("/api/wiki/page")
async def api_wiki_get_page(path: str):
    content = get_article(path)
    if content is None: raise HTTPException(status_code=404, detail=f"Article not found: {path}")
    return JSONResponse({"path":path,"content":content})

@router.delete("/api/wiki/page")
async def api_wiki_delete_page(request: Request, path: str):
    from utils.auth import check_admin
    if not check_admin(request): raise HTTPException(status_code=401, detail="Admin required")
    ok = delete_article(path)
    if not ok: raise HTTPException(status_code=404, detail=f"Article not found: {path}")
    return JSONResponse({"ok":True})

@router.post("/api/wiki/rebuild-index")
async def api_wiki_rebuild_index(): return JSONResponse(rebuild_index())

@router.get("/api/wiki/raw")
async def api_wiki_list_raw(topic: str = None):
    files = list_raw(topic=topic)
    return JSONResponse({"count":len(files),"files":files})

@router.get("/api/wiki/raw-file")
async def api_wiki_get_raw(path: str):
    content = get_raw(path)
    if content is None: raise HTTPException(status_code=404, detail=f"File not found: {path}")
    return JSONResponse({"path":path,"content":content})
