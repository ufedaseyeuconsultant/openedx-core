"""
Tests that the tagging signals are sent with tagging's base model classes as senders, for a CBE taxonomy.
"""
import pytest

from openedx_learning.models_api import CompetencyTaxonomy
from openedx_tagging import api
from openedx_tagging.models import ObjectTag, Tag, Taxonomy
from openedx_tagging.signals import object_tags_archived, tags_archived, taxonomies_archived
from tests.openedx_tagging.utils import received

pytestmark = pytest.mark.django_db


def test_signal_senders_are_the_base_classes() -> None:
    """The sender is ``ObjectTag``, ``Tag`` or ``Taxonomy``, whatever subclass of ``Taxonomy`` the operation ran on."""
    taxonomy = api.create_taxonomy("Competencies", taxonomy_cls=CompetencyTaxonomy)
    locked_tag = api.add_tag_to_taxonomy(taxonomy, "locked")
    api.tag_object("locked-object", taxonomy, ["locked"])
    object_tag = ObjectTag.objects.get(object_id="locked-object")
    api.lock_object_tags_for_deletion(object_tag.pk)

    with (
        received(object_tags_archived, sender=ObjectTag) as object_tag_calls,
        received(tags_archived, sender=Tag) as tag_calls,
        received(taxonomies_archived, sender=Taxonomy) as taxonomy_calls,
    ):
        api.delete_object_tags("locked-object")
        api.delete_tags_from_taxonomy(taxonomy, ["locked"], with_subtags=False)
        api.delete_taxonomy(taxonomy)

    assert [call["object_tag_ids"] for call in object_tag_calls] == [[object_tag.pk]]
    assert [call["tag_ids"] for call in tag_calls] == [[locked_tag.pk]]
    assert [call["taxonomy_ids"] for call in taxonomy_calls] == [[taxonomy.pk]]
