from typing import List, Optional

from pydantic import BaseModel


class UserCreate(BaseModel):
    username: str
    # Optional password — when provided the admin is *actually*
    # setting a usable login password for the new account instead of
    # generating a random one (the old behaviour, see
    # ``UserService.create_user``). Leave empty / None to keep the
    # legacy random-password behaviour for back-compat scripts.
    password: Optional[str] = None
    email: Optional[str] = None
    quota_5h: Optional[int] = 3000
    quota_week: Optional[int] = 5000


class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    email: Optional[str] = None
    quota_5h: Optional[int] = None
    quota_week: Optional[int] = None
    quota_month: Optional[int] = None
    monthly_budget: Optional[float] = None
    is_active: Optional[bool] = None
    # Optimistic-lock token. Callers that don't care about concurrent
    # edits (legacy / admin single-tab) leave this as None — the
    # service then skips the version check. When set, the UPDATE is
    # guarded by ``AND version = ?`` and the row must currently have
    # that version; otherwise ValueError is raised.
    version: Optional[int] = None


class UserResponse(BaseModel):
    id: int
    username: str
    api_key: str
    quota_5h: int
    quota_week: int
    usage_5h: int
    usage_week: int
    is_active: bool
    created_at: str
    # Echoes the current row version so the next edit can pass it back
    # as the optimistic-lock token. Defaults to 1 for rows created
    # before migration 37 backfilled the column.
    version: int = 1
    # Only populated by UserService.create_user() when the admin did
    # not supply a password — the SPA shows it once in a "save these
    # credentials" dialog. Field is optional / model_extra-friendly
    # so existing callers (admin list / update) keep returning the
    # same shape without the extra key.
    generated_password: Optional[str] = None


class AuthUserContext(BaseModel):
    id: int
    api_key: str
    quota_5h: int
    quota_week: int
    is_active: bool


class ProxyRequest(BaseModel):
    model: str = "MiniMax-M1"
    messages: List[dict]
    stream: bool = False
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.7


class UsageStats(BaseModel):
    total_requests: int
    total_tokens: int
    avg_response_time: float
    success_rate: float


class AdminLogin(BaseModel):
    username: str
    password: str


class ChatRequest(BaseModel):
    session_id: str
    message: str
    model: str = "MiniMax-M1"
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.7


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    tokens_used: int
