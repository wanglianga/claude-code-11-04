from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .init_db import init_db
from .routers import events as events_router
from .routers import reconciliation as recon_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="订单到清结算差异处理中心",
    description=(
        "四层架构（原始事件/标准账本/结算匹配/月度汇总），"
        "幂等摄入、乱序安全、只追加账本、人工处置与不可变快照。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(events_router.router)
app.include_router(recon_router.router)


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}
