import asyncio
import hashlib
import json
import mimetypes
import os
import weakref
import zipfile
from asyncio import to_thread
from datetime import datetime
from datetime import timedelta
from functools import partial
from importlib import import_module
from io import BytesIO
from time import mktime
from types import SimpleNamespace
from typing import AsyncGenerator
from urllib.parse import urlparse
from wsgiref.handlers import format_date_time

import docker
import httpx
import py7zr
import yaml
from fastapi import APIRouter
from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

clients = weakref.WeakSet()


with open("database.yaml", "rt") as f:
    database = yaml.safe_load(f)


async def online(clients: set) -> None:
    message = json.dumps({"event": {"topic": "online", "data": {"clients": len(clients)}}})
    await asyncio.gather(*(client.send(message) for client in clients))


broadcast = SimpleNamespace(online=online)


async def add(connection: WebSocket) -> None:
    clients.add(connection)
    await broadcast.online(clients)


async def disconnect(connection: WebSocket) -> None:
    clients.discard(connection)
    await broadcast.online(clients)


@app.websocket("/socket")
async def websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    clients.add(websocket)

    try:

        async def ping() -> None:
            while True:
                try:
                    await asyncio.sleep(10)
                    await websocket.send_text(json.dumps({"command": "ping"}))
                except (WebSocketDisconnect, asyncio.TimeoutError):
                    await disconnect(websocket)
                    break

        async def relay() -> None:
            try:
                async for message in websocket.iter_text():
                    match json.loads(message):
                        case {"rpc": {"request": {"id": id, "method": method, "arguments": arguments}}}:
                            response = {"rpc": {"response": {"id": id}}}
                            try:
                                module = import_module(f"procedures.{method}")
                                func = partial(module.run, **arguments)
                                result = await to_thread(func)
                                response["rpc"]["response"]["result"] = result
                            except Exception as exc:
                                response["rpc"]["response"]["error"] = str(exc)
                            await websocket.send_text(json.dumps(response))
                        case _:
                            pass
            except WebSocketDisconnect:
                await disconnect(websocket)

        await asyncio.gather(ping(), relay())
    finally:
        await disconnect(websocket)


redis = None
client = docker.from_env()
container = client.containers.get("redis")
hostname = container.attrs["Config"]["Hostname"]


@app.on_event("startup")
async def startup_event():
    global redis
    redis = Redis(host=hostname, port=6379, decode_responses=False)
    await redis.ping()


@app.on_event("shutdown")
async def shutdown_event():
    global redis
    if redis:
        await redis.close()


async def get_redis() -> Redis:
    if not redis:
        raise RuntimeError("Redis client is not initialized.")
    return redis


async def download(
    redis: Redis,
    url: str,
    filename: str,
    ttl: timedelta = timedelta(days=365),
) -> tuple[AsyncGenerator[bytes, None], str] | None:
    namespace = url.split("://", 1)[-1]

    def key(parts: tuple[str, ...]) -> str:
        return ":".join(parts)

    async with redis.pipeline(transaction=True) as pipe:
        pipe.get(key((namespace, filename, "content")))
        pipe.get(key((namespace, filename, "hash")))
        data, hash = await pipe.execute()

    match data, hash:
        case (bytes() as data, bytes() as hash) if all(value and value.strip() for value in (data, hash)):

            async def stream():
                yield data

            return stream(), hash.decode()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url)
        if not response.is_success:
            return None

        ext = os.path.splitext(urlparse(url).path)[-1].lower()

        async with redis.pipeline(transaction=True) as pipe:

            def store(pipe, key_parts: tuple[str, ...], content: bytes, hash: str) -> None:
                prefix = key(key_parts)
                pipe.set(key((prefix, "hash")), hash, ex=ttl)
                pipe.set(key((prefix, "content")), content, ex=ttl)

            match ext:
                case ".zip":
                    with zipfile.ZipFile(BytesIO(response.content)) as zf:
                        result = None
                        for name in zf.namelist():
                            content = zf.read(name)
                            content_hash = hashlib.sha1(content).hexdigest()
                            store(pipe, (namespace, name), content, content_hash)
                            if name == filename:

                                async def stream():
                                    yield content

                                result = (stream(), content_hash)

                        await pipe.execute()
                        return result

                case _:
                    data = response.content
                    content_hash = hashlib.sha1(data).hexdigest()
                    store(pipe, (namespace, filename), data, content_hash)

                    await pipe.execute()

                    async def stream():
                        yield data

                    return stream(), content_hash


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    artifacts = database.get("artifacts", [])

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"artifacts": artifacts},
    )


router = APIRouter(prefix="/play/{runtime}/{organization}/{repository}/{release}/{resolution}")


@router.get("/", response_class=HTMLResponse)
async def play(
    runtime: str,
    organization: str,
    repository: str,
    release: str,
    resolution: str,
    request: Request,
):
    mapping = {
        "480p": (854, 480),
        "720p": (1280, 720),
        "1080p": (1920, 1080),
    }
    width, height = mapping[resolution]

    url = f"/play/{runtime}/{organization}/{repository}/{release}/{resolution}/"

    return templates.TemplateResponse(
        request=request,
        name="play.html",
        context={
            "url": url,
            "width": width,
            "height": height,
        },
    )


@router.get("/{filename}")
async def dynamic(
    runtime: str,
    organization: str,
    repository: str,
    release: str,
    resolution: str,
    filename: str,
    redis: Redis = Depends(get_redis),
):
    match filename:
        case "bundle.7z":
            url = f"https://github.com/{organization}/{repository}/releases/download/v{release}/bundle.7z"
        case "carimbo.js" | "carimbo.wasm":
            url = f"https://github.com/carimbolabs/carimbo/releases/download/v{runtime}/WebAssembly.zip"
        case _:
            raise HTTPException(status_code=404)

    result = await download(redis, url, filename)
    if result is None:
        raise HTTPException(status_code=404)

    content, hash = result
    media_type, _ = mimetypes.guess_type(filename)
    if not media_type:
        media_type = "application/octet-stream"

    now = datetime.utcnow()
    timestamp = mktime(now.timetuple())
    modified = format_date_time(timestamp)

    duration = timedelta(days=365).total_seconds()
    headers = {
        "Cache-Control": f"public, max-age={int(duration)}, immutable",
        "Content-Disposition": f'inline; filename="{filename}"',
        "Last-Modified": modified,
        "ETag": hash,
    }

    return StreamingResponse(
        content=content,
        media_type=media_type,
        headers=headers,
    )


app.include_router(router)


debugger = APIRouter(prefix="/debug")


@debugger.get("/")
async def serve_debug_html(request: Request):
    return templates.TemplateResponse("debug.html", context={"request": request})


@debugger.get("/bundle.7z")
async def serve_bundle_7z():
    stream = BytesIO()
    with py7zr.SevenZipFile(stream, mode="w") as archive:
        archive.writeall("/opt/game", arcname=".")
    stream.seek(0)

    return Response(
        content=stream.read(),
        media_type="application/x-7z-compressed",
        headers={"Content-Disposition": "attachment; filename=bundle.7z"},
    )


@debugger.get("/{filename}")
async def serve_engine_file(filename: str):
    path = "/opt/engine"
    match filename:
        case "carimbo.js":
            return FileResponse(f"{path}/{filename}", media_type="application/javascript")
        case "carimbo.wasm":
            return FileResponse(f"{path}/{filename}", media_type="application/wasm")
        case _:
            return Response(content="File not found", status_code=404)


app.include_router(debugger)
