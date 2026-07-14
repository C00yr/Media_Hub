from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes import FavoritePayload, add_favorite, list_favorites, remove_favorite
from app.db.session import Base
from app.models.entities import User


def test_favorite_add_list_and_remove_are_user_scoped():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    try:
        first_user = User(username="first", password_hash="x", role="user")
        second_user = User(username="second", password_hash="x", role="user")
        db.add_all([first_user, second_user])
        db.commit()
        db.refresh(first_user)
        db.refresh(second_user)

        request = FavoritePayload(
            media={
                "id": "movie-157336",
                "tmdb_id": 157336,
                "media_type": "movie",
                "title": "星际穿越",
                "year": "2014",
                "poster": "/api/tmdb/image/w342/test.jpg",
                "rating": 8.7,
            }
        )
        created = add_favorite(request, first_user, db)
        duplicate = add_favorite(request, first_user, db)

        assert created["created"] is True
        assert duplicate["created"] is False
        assert list_favorites(first_user, db)["count"] == 1
        assert list_favorites(second_user, db)["count"] == 0
        assert remove_favorite("movie", 157336, first_user, db)["removed"] is True
        assert list_favorites(first_user, db)["count"] == 0
    finally:
        db.close()
