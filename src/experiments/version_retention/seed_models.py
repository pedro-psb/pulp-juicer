"""Seed file content and memberships using PostgreSQL set operations, without artifacts."""

import os
import sys


def seed(repository_name: str, count: int = 500_000) -> str:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pulpcore.app.settings")

    import django

    django.setup()

    from django.db import connection, transaction
    from pulp_file.app.models import FileContent, FileRepository
    from pulpcore.app.models import RepositoryVersion

    with transaction.atomic():
        repository = FileRepository.objects.get(name=repository_name)
        version = RepositoryVersion.objects.create(
            repository=repository, number=1, complete=True
        )
        repository.next_version = 2
        repository.save(update_fields=["next_version"])
        with connection.cursor() as cursor:
            cursor.execute(
                """CREATE TEMP TABLE retention_seed ON COMMIT DROP AS
                   SELECT gen_random_uuid() AS id,
                          'seed/' || lpad(n::text, 6, '0') || '.bin' AS relative_path
                   FROM generate_series(0, %s - 1) AS n""",
                [count],
            )
            cursor.execute(
                """INSERT INTO core_content
                   (pulp_id, pulp_created, pulp_last_updated, pulp_type,
                    pulp_labels, timestamp_of_interest, pulp_domain_id)
                   SELECT id, now(), now(), %s, ''::hstore, now(), %s
                   FROM retention_seed""",
                [FileContent.get_pulp_type(), repository.pulp_domain_id],
            )
            cursor.execute(
                """INSERT INTO file_filecontent
                   (content_ptr_id, relative_path, digest, _pulp_domain_id)
                   SELECT id, relative_path, repeat('0', 64), %s
                   FROM retention_seed""",
                [repository.pulp_domain_id],
            )
            cursor.execute(
                """INSERT INTO core_repositorycontent
                   (pulp_id, pulp_created, pulp_last_updated,
                    content_id, repository_id, version_added_id)
                   SELECT gen_random_uuid(), now(), now(), id, %s, %s
                   FROM retention_seed""",
                [repository.pk, version.pk],
            )
            cursor.execute(
                """UPDATE core_repositoryversion
                   SET content_ids = (SELECT array_agg(id) FROM retention_seed)
                   WHERE pulp_id = %s""",
                [version.pk],
            )
            cursor.execute(
                """INSERT INTO core_repositoryversioncontentdetails
                   (count_type, content_type, repository_version_id, count)
                   VALUES ('A', %s, %s, %s), ('P', %s, %s, %s)""",
                [
                    FileContent.get_pulp_type(),
                    version.pk,
                    count,
                    FileContent.get_pulp_type(),
                    version.pk,
                    count,
                ],
            )
            cursor.execute(
                "SELECT id FROM retention_seed WHERE relative_path = 'seed/000000.bin'"
            )
            first_id = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM core_repositorycontent WHERE repository_id = %s",
                [repository.pk],
            )
            memberships = cursor.fetchone()[0]
            cursor.execute(
                "SELECT cardinality(content_ids) FROM core_repositoryversion WHERE pulp_id = %s",
                [version.pk],
            )
            content_ids = cursor.fetchone()[0]
            if memberships != count or content_ids != count:
                raise RuntimeError(
                    f"Seeded {memberships} memberships and {content_ids} IDs, expected {count}"
                )
        repository.cleanup_old_versions()
        actual = repository.latest_version().count()
        if actual != count:
            raise RuntimeError(f"Seeded {actual} members, expected {count}")
        return str(first_id)


if __name__ == "__main__":
    print(seed(sys.argv[1]))
