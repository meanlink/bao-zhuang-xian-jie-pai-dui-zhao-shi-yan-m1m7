from pydantic import BaseModel, Field
class ReviewItemInput(BaseModel):
    dataset_id: int
    shift: str = Field(max_length=20)
class ReviewGroupInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    confidence: float = Field(default=.95)
    alpha: float = Field(default=.05)
    items: list[ReviewItemInput] = Field(default_factory=list)
class ReviewLockInput(BaseModel):
    confidence: float | None = Field(default=None)
    alpha: float | None = Field(default=None)
    items: list[ReviewItemInput] | None = Field(default=None)
