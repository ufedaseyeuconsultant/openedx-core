"""
Tests for the signals that tagging sends when it archives associations, tags, or taxonomies.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings

from openedx_tagging import api
from openedx_tagging.models import ObjectTag, Tag, Taxonomy
from openedx_tagging.models.base import _retry_on_concurrent_write
from openedx_tagging.signals import object_tags_archived, tags_archived, taxonomies_archived

from .utils import WritesToPrimaryRouter, concurrent_write, received


class TestObjectTagsArchived(TestCase):
    """
    Tests for ``object_tags_archived``.
    """

    def setUp(self) -> None:
        super().setUp()
        self.taxonomy = api.create_taxonomy("Free text", allow_free_text=True)

    def _object_tag(self, value: str, *, locked: bool = False, archived: bool = False) -> ObjectTag:
        return ObjectTag.objects.create(
            object_id="obj",
            taxonomy=self.taxonomy,
            _value=value,
            deletion_locked=locked,
            archived=archived,
        )

    def test_object_tags_archived_sent_once_with_newly_archived_ids_only(self) -> None:
        locked = self._object_tag("locked", locked=True)
        self._object_tag("already archived", locked=True, archived=True)
        self._object_tag("unlocked")

        with received(object_tags_archived, sender=ObjectTag) as calls:
            api.delete_object_tags("obj")

        assert len(calls) == 1
        assert calls[0]["object_tag_ids"] == [locked.pk]
        assert calls[0]["sender"] is ObjectTag
        assert calls[0]["using"] == "default"

    def test_object_tags_archived_not_sent_when_nothing_newly_archived(self) -> None:
        self._object_tag("unlocked")
        with received(object_tags_archived) as calls:
            api.delete_object_tags("obj")
        assert not calls

        self._object_tag("already archived", locked=True, archived=True)
        with received(object_tags_archived) as calls:
            api.delete_object_tags("obj")
        assert not calls

    def test_signal_sent_after_the_hard_delete(self) -> None:
        self._object_tag("locked", locked=True)
        unlocked = self._object_tag("unlocked")
        seen: list[bool] = []

        def receiver(**kwargs: Any) -> None:
            seen.append(ObjectTag.objects.filter(pk=unlocked.pk).exists())

        object_tags_archived.connect(receiver, weak=False, dispatch_uid="test-after-delete")
        try:
            api.delete_object_tags("obj")
        finally:
            object_tags_archived.disconnect(receiver, dispatch_uid="test-after-delete")

        assert seen == [False]


class TestTagsArchived(TestCase):
    """
    Tests for ``tags_archived``.
    """

    def setUp(self) -> None:
        super().setUp()
        self.taxonomy = api.create_taxonomy("Closed")
        for value in ("locked", "other", "plain"):
            api.add_tag_to_taxonomy(self.taxonomy, value)

    def _lock(self, value: str) -> None:
        api.tag_object("locked-object", self.taxonomy, [value])
        api.lock_object_tags_for_deletion(ObjectTag.objects.get(object_id="locked-object", tag__value=value).pk)

    def test_tags_archived_sent_once_with_newly_archived_ids_only(self) -> None:
        self._lock("locked")
        self._lock("other")
        api.delete_tags_from_taxonomy(self.taxonomy, ["other"], with_subtags=False)  # Archives "other" ahead of time
        locked_pk = self.taxonomy.tag_set.get(value="locked").pk

        with received(tags_archived, sender=Tag) as calls:
            api.delete_tags_from_taxonomy(self.taxonomy, ["locked", "other"], with_subtags=False)

        assert len(calls) == 1
        assert calls[0]["tag_ids"] == [locked_pk]
        assert calls[0]["sender"] is Tag
        assert calls[0]["using"] == "default"

    def test_tags_archived_sent_on_the_delete_path_too(self) -> None:
        self._lock("locked")
        locked_pk = self.taxonomy.tag_set.get(value="locked").pk

        with received(tags_archived) as calls:
            api.delete_tags_from_taxonomy(self.taxonomy, ["locked", "plain"], with_subtags=False)

        assert [call["tag_ids"] for call in calls] == [[locked_pk]]
        assert not self.taxonomy.tag_set.filter(value="plain").exists()

    def test_tags_archived_not_sent_when_nothing_is_archived(self) -> None:
        with received(tags_archived) as calls:
            api.delete_tags_from_taxonomy(self.taxonomy, ["plain"], with_subtags=False)
        assert not calls

    def test_tags_archived_sent_once_across_a_concurrent_write_retry(self) -> None:
        self._lock("locked")
        locked = self.taxonomy.tag_set.get(value="locked")

        def add_sub_tag() -> None:
            Tag.objects.get_or_create(taxonomy=self.taxonomy, value="Kid", parent=locked)

        with received(tags_archived) as calls:
            with concurrent_write(Tag, add_sub_tag, before="update"):
                api.delete_tags_from_taxonomy(self.taxonomy, ["locked"], with_subtags=True)

        assert [call["tag_ids"] for call in calls] == [[locked.pk]]
        assert not Tag.objects.filter(taxonomy=self.taxonomy, value="Kid").exists()


class TestTaxonomiesArchived(TestCase):
    """
    Tests for ``taxonomies_archived``.
    """

    def _locked_taxonomy(self) -> Taxonomy:
        """Return a taxonomy with a deletion-locked association, so that deleting it archives it."""
        taxonomy = api.create_taxonomy("Closed")
        api.add_tag_to_taxonomy(taxonomy, "locked")
        api.tag_object("locked-object", taxonomy, ["locked"])
        api.lock_object_tags_for_deletion(ObjectTag.objects.get(object_id="locked-object").pk)
        return taxonomy

    def test_taxonomies_archived_sent_once_and_only_on_change(self) -> None:
        taxonomy = self._locked_taxonomy()

        with received(taxonomies_archived, sender=Taxonomy) as calls:
            api.delete_taxonomy(taxonomy)

            assert len(calls) == 1
            assert calls[0]["taxonomy_ids"] == [taxonomy.pk]
            assert calls[0]["sender"] is Taxonomy
            assert calls[0]["using"] == "default"

            api.delete_taxonomy(taxonomy)

        assert len(calls) == 1
        assert taxonomy.archived is True

    def test_taxonomies_archived_not_sent_when_the_taxonomy_is_deleted(self) -> None:
        taxonomy = api.create_taxonomy("Unlocked")

        with received(taxonomies_archived) as calls:
            api.delete_taxonomy(taxonomy)

        assert not calls
        assert not Taxonomy.objects.filter(pk=taxonomy.pk).exists()

    def test_receiver_error_rolls_back_the_archive(self) -> None:
        taxonomy = self._locked_taxonomy()

        def failing_receiver(**kwargs: Any) -> None:
            raise RuntimeError("receiver failed")

        taxonomies_archived.connect(failing_receiver, weak=False, dispatch_uid="test-failing")
        try:
            with self.assertRaises(RuntimeError):
                api.delete_taxonomy(taxonomy)
        finally:
            taxonomies_archived.disconnect(failing_receiver, dispatch_uid="test-failing")

        assert Taxonomy.objects.get(pk=taxonomy.pk).archived is False


class TestSignalDatabaseAlias(TestCase):
    """
    ``object_tags_archived`` carries the alias of the database the archive was written on, as the router chose it.
    """

    def test_object_tags_archived_carries_the_write_alias_when_reads_are_routed_elsewhere(self) -> None:
        taxonomy = api.create_taxonomy("Free text", allow_free_text=True)
        locked = ObjectTag.objects.create(
            object_id="obj", taxonomy=taxonomy, _value="locked", deletion_locked=True
        )

        with override_settings(DATABASE_ROUTERS=[WritesToPrimaryRouter()]), received(object_tags_archived) as calls:
            locked.delete()

        assert [call["using"] for call in calls] == ["default"]


class TestRetryWindowAlias(TestCase):
    """
    The transaction that a receiver of ``tags_archived`` runs in is opened on the alias the signal reports, so a
    receiver error rolls back the archive on that alias.
    """

    def test_the_retry_helper_opens_its_transaction_on_the_alias_it_is_given(self) -> None:
        with patch.object(transaction, "atomic", wraps=transaction.atomic) as atomic:
            _retry_on_concurrent_write(lambda: None, using="default")

        atomic.assert_called_once_with(using="default")

    def test_delete_tags_retries_on_the_alias_it_reports_in_the_signal(self) -> None:
        taxonomy = api.create_taxonomy("Closed")
        api.add_tag_to_taxonomy(taxonomy, "locked")
        api.tag_object("locked-object", taxonomy, ["locked"])
        api.lock_object_tags_for_deletion(ObjectTag.objects.get(object_id="locked-object").pk)

        with (
            patch("openedx_tagging.models.base._retry_on_concurrent_write", wraps=_retry_on_concurrent_write) as retry,
            received(tags_archived) as calls,
        ):
            api.delete_tags_from_taxonomy(taxonomy, ["locked"], with_subtags=False)

        assert [call["using"] for call in calls] == ["default"]
        assert retry.call_args.kwargs == {"using": "default"}
