"""
JSON:API 规范的数据结构定义
使用 dataclass 定义所有 API 返回的数据结构
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Union


@dataclass
class Link:
    """链接信息"""

    self: Optional[str] = None
    related: Optional[str] = None
    first: Optional[str] = None
    last: Optional[str] = None
    prev: Optional[str] = None
    next: Optional[str] = None


@dataclass
class Links:
    """链接集合"""

    self: Optional[Union[str, Link]] = None
    related: Optional[Union[str, Link]] = None
    first: Optional[Union[str, Link]] = None
    last: Optional[Union[str, Link]] = None
    prev: Optional[Union[str, Link]] = None
    next: Optional[Union[str, Link]] = None


@dataclass
class ResourceIdentifier:
    """资源标识符对象"""

    type: str
    id: str


@dataclass
class Relationship:
    """关系对象"""

    data: Optional[
        Union[ResourceIdentifier, List[ResourceIdentifier], Dict[str, Any]]
    ] = None
    links: Optional[Links] = None
    meta: Optional[Dict[str, Any]] = None


@dataclass
class ResourceAttributes:
    """资源属性基类"""

    pass


@dataclass
class NoteAttributes(ResourceAttributes):
    """笔记属性"""

    guid: str
    note_type: str
    tags: List[str]
    fields: Dict[str, str]
    created: int
    modified: int


@dataclass
class CardAttributes(ResourceAttributes):
    """卡片属性"""

    note_id: int
    deck_id: int
    deck_name: str
    template_index: int
    type: int
    queue: int
    interval: int
    ease_factor: int
    reviews: int
    lapses: int
    due: int


@dataclass
class DeckAttributes(ResourceAttributes):
    """牌组属性"""

    name: str
    description: str
    review_count: int
    new_count: int
    learning_count: int
    total_cards: int
    total_notes: int
    deck_config_id: int
    deck_config_name: str
    created: int
    modified: int


@dataclass
class NoteTypeAttributes(ResourceAttributes):
    """笔记类型属性"""

    name: str
    fields: List[str]
    templates: List[str]


@dataclass
class Resource:
    """资源对象"""

    type: str
    id: str
    attributes: Optional[
        Union[NoteAttributes, CardAttributes, DeckAttributes, NoteTypeAttributes]
    ] = None
    relationships: Optional[Dict[str, Relationship]] = None
    links: Optional[Links] = None
    meta: Optional[Dict[str, Any]] = None


@dataclass
class ResourceCollection:
    """资源集合"""

    data: List[Resource]
    included: Optional[List[Resource]] = None
    links: Optional[Links] = None
    meta: Optional[Dict[str, Any]] = None


@dataclass
class SingleResource:
    """单个资源"""

    data: Optional[Resource] = None
    included: Optional[List[Resource]] = None
    links: Optional[Links] = None
    meta: Optional[Dict[str, Any]] = None


@dataclass
class JsonApiDocument:
    """JSON:API 文档基类"""

    data: Optional[Union[Resource, List[Resource], None]] = None
    errors: Optional[List[Dict[str, Any]]] = None
    meta: Optional[Dict[str, Any]] = None
    # jsonapi: Optional[Dict[str, Any]] = field(default_factory=lambda: {"version": "1.0"})
    links: Optional[Links] = None
    included: Optional[List[Resource]] = None


@dataclass
class NoteCollectionDocument(JsonApiDocument):
    """笔记集合文档"""

    data: List[Resource] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NoteDocument(JsonApiDocument):
    """单个笔记文档"""

    data: Optional[Resource] = None


@dataclass
class CardCollectionDocument(JsonApiDocument):
    """卡片集合文档"""

    data: List[Resource] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeckCollectionDocument(JsonApiDocument):
    """牌组集合文档"""

    data: List[Resource] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeckDocument(JsonApiDocument):
    """单个牌组文档"""

    data: Optional[Resource] = None


@dataclass
class NoteTypeCollectionDocument(JsonApiDocument):
    """笔记类型集合文档"""

    data: List[Resource] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Error:
    """错误对象"""

    id: Optional[str] = None
    status: Optional[str] = None
    code: Optional[str] = None
    title: Optional[str] = None
    detail: Optional[str] = None
    source: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None


@dataclass
class ErrorDocument(JsonApiDocument):
    """错误文档"""

    errors: List[Error] = field(default_factory=list)


@dataclass
class RootDocument(JsonApiDocument):
    """根路径文档"""

    data: None = None
    meta: Dict[str, Any] = field(
        default_factory=lambda: {"message": "Anki Restful API", "version": "1.0.4"}
    )
    links: Links = field(default_factory=lambda: Links(self="/api/"))


# 分页信息
@dataclass
class PaginationMeta:
    """分页元信息"""

    page: int
    limit: int
    total: int
    pages: int


# 创建资源的辅助函数
def create_note_resource(note_id: int, note_data: Dict[str, Any]) -> Resource:
    """创建笔记资源对象"""
    return Resource(
        type="notes",
        id=str(note_id),
        attributes=NoteAttributes(
            guid=note_data.get("guid", ""),
            note_type=note_data.get("note_type", ""),
            tags=note_data.get("tags", []),
            fields=note_data.get("fields", {}),
            created=note_data.get("created", 0),
            modified=note_data.get("modified", 0),
        ),
        links=Links(
            self=f"/api/notes/{note_id}", related=f"/api/notes/{note_id}/cards"
        ),
    )


def create_card_resource(card_id: int, card_data: Dict[str, Any]) -> Resource:
    """创建卡片资源对象"""
    return Resource(
        type="cards",
        id=str(card_id),
        attributes=CardAttributes(
            note_id=card_data.get("note_id", 0),
            deck_id=card_data.get("deck_id", 0),
            deck_name=card_data.get("deck_name", ""),
            template_index=card_data.get("template_index", 0),
            type=card_data.get("type", 0),
            queue=card_data.get("queue", 0),
            interval=card_data.get("interval", 0),
            ease_factor=card_data.get("ease_factor", 0),
            reviews=card_data.get("reviews", 0),
            lapses=card_data.get("lapses", 0),
            due=card_data.get("due", 0),
        ),
        relationships={
            "note": Relationship(
                data=ResourceIdentifier(
                    type="notes", id=str(card_data.get("note_id", 0))
                ),
                links=Links(
                    self=f"/api/cards/{card_id}/relationships/note",
                    related=f"/api/notes/{card_data.get('note_id', 0)}",
                ),
            ),
            "deck": Relationship(
                data=ResourceIdentifier(
                    type="decks", id=str(card_data.get("deck_id", 0))
                ),
                links=Links(
                    self=f"/api/cards/{card_id}/relationships/deck",
                    related=f"/api/decks/{card_data.get('deck_id', 0)}",
                ),
            ),
        },
        links=Links(self=f"/api/cards/{card_id}"),
    )


def create_deck_resource(deck_id: int, deck_data: Dict[str, Any]) -> Resource:
    """创建牌组资源对象"""
    return Resource(
        type="decks",
        id=str(deck_id),
        attributes=DeckAttributes(
            name=deck_data.get("name", ""),
            description=deck_data.get("description", ""),
            review_count=deck_data.get("review_count", 0),
            new_count=deck_data.get("new_count", 0),
            learning_count=deck_data.get("learning_count", 0),
            total_cards=deck_data.get("total_cards", 0),
            total_notes=deck_data.get("total_notes", 0),
            deck_config_id=deck_data.get("deck_config_id", 0),
            deck_config_name=deck_data.get("deck_config_name", ""),
            created=deck_data.get("created", 0),
            modified=deck_data.get("modified", 0),
        ),
        relationships={
            "notes": Relationship(
                links=Links(
                    self=f"/api/decks/{deck_id}/relationships/notes",
                    related=f"/api/decks/{deck_id}/notes",
                )
            )
        },
        links=Links(
            self=f"/api/decks/{deck_id}", related=f"/api/decks/{deck_id}/notes"
        ),
    )


def create_note_type_resource(
    note_type_id: int, note_type_data: Dict[str, Any]
) -> Resource:
    """创建笔记类型资源对象"""
    return Resource(
        type="notetypes",
        id=str(note_type_id),
        attributes=NoteTypeAttributes(
            name=note_type_data.get("name", ""),
            fields=note_type_data.get("fields", []),
            templates=note_type_data.get("templates", []),
        ),
        links=Links(self=f"/api/notetypes/{note_type_id}"),
    )
