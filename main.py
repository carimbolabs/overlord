import asyncio
import hashlib
import json
import mimetypes
import os
import weakref
import zipfile
from asyncio import to_thread
from datetime import timedelta
from functools import partial
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace
from typing import AsyncGenerator
from urllib.parse import urlparse

import docker
import httpx
from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
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


class Metadata(BaseModel):
    runtime: str
    organization: str
    repository: str
    release: str
    format: str
    remaining: str = ""


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


# url := fmt.Sprintf("https://github.com/flippingpixels/carimbo/releases/download/v%s/WebAssembly.zip", runtime)


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
    ttl: timedelta = timedelta(hours=1),
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

            def store(pipe, key_parts: tuple[str, ...], content: bytes, hash: str):
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


@app.get("/play/{runtime}/{organization}/{repository}/{release}/{format}", response_class=HTMLResponse)
async def index(runtime: str, organization: str, repository: str, release: str, format: str, request: Request):
    mapping = {
        "480p": (854, 480),
        "720p": (1280, 720),
        "1080p": (1920, 1080),
    }
    width, height = mapping[format]

    url = f"/play/{runtime}/{organization}/{repository}/{release}/{format}/"

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "url": url,
            "width": width,
            "height": height,
        },
    )


@app.get("/play/{runtime}/{organization}/{repository}/{release}/{format}/{filename}")
async def static(
    runtime: str,
    organization: str,
    repository: str,
    release: str,
    format: str,
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

    duration = timedelta(days=365).total_seconds()
    headers = {
        "Cache-Control": f"public, max-age={int(duration)}",
        "Content-Disposition": f'inline; filename="{filename}"',
        "ETag": hash,
    }

    return StreamingResponse(
        content=content,
        media_type=media_type,
        headers=headers,
    )
