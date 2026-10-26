from sqlalchemy import select

from app.db import SessionLocal
from app.models import Org, User


def main():
    with SessionLocal() as db:
        org = db.scalar(select(Org).where(Org.name == "Demo Clinic"))
        if org is None:
            org = Org(name="Demo Clinic")
            db.add(org)
            db.flush()
            db.add(
                User(
                    org_id=org.id,
                    email="rep@democlinic.test",
                    name="Demo Rep",
                    role="admin",
                )
            )
            db.commit()
        print("Org id:", org.id)


if __name__ == "__main__":
    main()