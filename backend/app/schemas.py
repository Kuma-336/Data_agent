from pydantic import BaseModel, EmailStr, Field


class RegisterInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)


class LoginInput(RegisterInput):
    pass


class UserOut(BaseModel):
    id: str
    email: EmailStr


class DataSourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str
    port: int = 5432
    database_name: str
    username: str
    password: str = Field(min_length=1)
    sslmode: str = "prefer"


class DataSourceOut(BaseModel):
    id: str
    name: str
    host: str
    port: int
    database_name: str
    username: str
    sslmode: str


class ChatCreate(BaseModel):
    title: str = "New analysis"


class ChatMessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    file_ids: list[str] = []
    connection_ids: list[str] = []


class ChartSpec(BaseModel):
    query_id: str
    title: str
    chart_type: str
    x_key: str
    y_keys: list[str]
