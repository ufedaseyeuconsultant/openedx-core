""""
Tagging API

Anyone using the openedx_tagging app should use these APIs instead of creating
or modifying the models directly, since there might be other related model
changes that you may not know about.

No permissions/rules are enforced by these methods -- these must be enforced in the views.

Please look at the models.py file for more information about the kinds of data
are stored in this app.
"""
from __future__ import annotations

import operator
from collections import Counter, defaultdict
from collections.abc import Iterable
from enum import Enum
from functools import partial
from typing import Any, cast

from django.db import models, router, transaction
from django.db.models import F, Q, QuerySet, Value
from django.db.models.functions import Coalesce, Concat, Lower
from django.utils.text import slugify
from django.utils.translation import gettext as _

from .data import TagDataQuerySet
from .models import ObjectTag, Tag, Taxonomy
from .models.base import ConcurrentDeleteError as _ConcurrentDeleteError
from .models.base import _ConcurrentWrite, _retry_on_concurrent_write
from .signals import taxonomies_archived

# Export this as part of the API
TagDoesNotExist = Tag.DoesNotExist
ObjectTagDoesNotExist = ObjectTag.DoesNotExist
ConcurrentDeleteError = _ConcurrentDeleteError

# Maximum number of tags allowed on any one object
OBJECT_MAX_TAGS = 100


class TaxonomyType(Enum):
    """
    Valid values for a taxonomy's type on create.
    """

    TAGS = "tags"
    COMPETENCY = "competency"


def create_taxonomy(  # pylint: disable=too-many-positional-arguments
    name: str,
    description: str | None = None,
    enabled=True,
    allow_multiple=True,
    allow_free_text=False,
    read_only=False,
    export_id: str | None = None,
    *,
    taxonomy_cls: type[Taxonomy] = Taxonomy,
) -> Taxonomy:
    """
    Creates, saves, and returns a new Taxonomy with the given attributes.

    If `export_id` is not given, one is auto-generated from the current
    Taxonomy count and a slug of `name`.

    Pass `taxonomy_cls` to create a subclass instance instead (e.g. a
    multi-table-inheritance child): building it fresh with every field, in one
    full_clean()+save(), writes both tables correctly without a separate step.
    """
    if not export_id:
        export_id = f"{Taxonomy.objects.count() + 1}-{slugify(name, allow_unicode=True)}"

    taxonomy = taxonomy_cls(
        name=name,
        description=description or "",
        enabled=enabled,
        allow_multiple=allow_multiple,
        allow_free_text=allow_free_text,
        read_only=read_only,
        export_id=export_id,
    )

    taxonomy.full_clean()
    taxonomy.save()
    return taxonomy


def get_taxonomy(taxonomy_id: int) -> Taxonomy | None:
    """
    Returns the Taxonomy which has the given ID, or None if not found or archived.
    """
    return Taxonomy.objects.unarchived().filter(pk=taxonomy_id).first()


def get_taxonomy_by_export_id(taxonomy_export_id: str) -> Taxonomy | None:
    """
    Returns the Taxonomy which has the given export ID, or None if not found or archived.
    """
    return Taxonomy.objects.unarchived().filter(export_id=taxonomy_export_id).first()


def get_taxonomies(enabled=True) -> QuerySet[Taxonomy]:
    """
    Returns a queryset containing the enabled, unarchived taxonomies, sorted by name.

    We return a QuerySet here for ease of use with Django Rest Framework and other query-based use cases.

    If you want the disabled taxonomies, pass enabled=False.
    If you want all taxonomies (both enabled and disabled), pass enabled=None.
    """
    queryset = Taxonomy.objects.unarchived().order_by("name", "id")
    if enabled is None:
        return queryset.all()
    return queryset.filter(enabled=enabled)


def get_tags(taxonomy: Taxonomy) -> TagDataQuerySet:
    """
    Returns a QuerySet of all the unarchived tags in the given taxonomy.

    Note that if the taxonomy is dynamic or free-text, only tags that have
    already been applied to some object will be returned.
    """
    return taxonomy.get_filtered_tags()


def get_root_tags(taxonomy: Taxonomy) -> TagDataQuerySet:
    """
    Returns a list of the root tags for the given taxonomy.

    Note that if the taxonomy allows free-text tags, then the returned list will be empty.
    """
    return taxonomy.get_filtered_tags(depth=1)


def search_tags(
    taxonomy: Taxonomy,
    search_term: str,
    exclude_object_id: str | None = None,
) -> TagDataQuerySet:
    """
    Returns a list of all tags that contains `search_term` of the given
    taxonomy, as well as their ancestors (so they can be displayed in a tree).

    If exclude_object_id is set, any tags applied to that object will be
    excluded from the results, e.g. to power an autocomplete search when adding
    additional tags to an object.
    """
    excluded_values = None
    if exclude_object_id:
        # Fetch tags that the object already has to exclude them from the result.
        # Note: this adds a fair bit of complexity. In the future, maybe we can just do this filtering on the frontend?
        excluded_values = list(
            taxonomy.objecttag_set.unarchived().filter(object_id=exclude_object_id).values_list(
                "_value", flat=True
            )
        )
    qs = taxonomy.get_filtered_tags(
        search_term=search_term,
        excluded_values=excluded_values,
    )
    return qs


def get_children_tags(
    taxonomy: Taxonomy,
    parent_tag_value: str,
) -> TagDataQuerySet:
    """
    Returns a QuerySet of unarchived children tags for the given parent tag.

    Raises Tag.DoesNotExist if the parent tag does not exist, or Tag.IsArchived if it is archived.

    Note that if the taxonomy allows free-text tags, then the returned list will be empty.
    """
    return taxonomy.get_filtered_tags(parent_tag_value=parent_tag_value, depth=1)


def get_ancestor_tags(tag: Tag) -> QuerySet[Tag]:
    """
    Return every ancestor of ``tag`` (its parent, grandparent, ... up to the root).

    Archived tags, and every tag of an archived taxonomy, are left out.

    Returns an empty queryset if ``tag``'s taxonomy is unset or allows free text, since
    free-text tags have no taxonomy-defined hierarchy.
    """
    if tag.taxonomy is None or tag.taxonomy.allow_free_text:
        return Tag.objects.none()
    return Tag.objects.unarchived().filter(taxonomy=tag.taxonomy, value__in=tag.get_lineage()[:-1])


def get_descendant_tags(tag: Tag) -> QuerySet[Tag]:
    """
    Return every descendant of ``tag`` at any depth, excluding ``tag`` itself.

    Archived tags, and every tag of an archived taxonomy, are left out.

    Returns an empty queryset if ``tag``'s taxonomy is unset or allows free text, since
    free-text tags have no taxonomy-defined hierarchy.
    """
    if tag.taxonomy is None or tag.taxonomy.allow_free_text:
        return Tag.objects.none()
    return Tag.objects.unarchived().filter(
        taxonomy=tag.taxonomy, depth__gt=tag.depth, lineage__startswith=tag.lineage
    )


def resync_object_tags(object_tags: QuerySet | None = None) -> int:
    """
    Reconciles ObjectTag entries with any changes made to their associated taxonomies and tags.

    By default, we iterate over all ObjectTags. Pass a filtered ObjectTags queryset to limit which tags are resynced.
    """
    if not object_tags:
        object_tags = ObjectTag.objects.select_related("tag", "taxonomy")

    # The fields resync() can change, each with the attribute that holds its stored value.
    resynced_fields = {"taxonomy": "taxonomy_id", "tag": "tag_id", "_value": "_value", "_export_id": "_export_id"}
    num_changed = 0
    for object_tag in object_tags:
        before = {field: getattr(object_tag, attribute) for field, attribute in resynced_fields.items()}
        changed = object_tag.resync()
        if changed:
            # Only the columns this resync changed are written, so that nothing set since the row was read, such
            # as a deletion lock or a repaired tag, is overwritten with the stale value.
            object_tag.save(update_fields=[
                field for field, attribute in resynced_fields.items() if getattr(object_tag, attribute) != before[field]
            ])
            num_changed += 1

    return num_changed


def get_object_tags(
    object_id: str,
    taxonomy_id: int | None = None,
    include_deleted: bool = False,
) -> QuerySet[ObjectTag]:
    """
    Returns a Queryset of object tags for a given object.

    Archived object tags are excluded, as are those whose tag or taxonomy is archived.

    Pass taxonomy_id to limit the returned object_tags to a specific taxonomy.
    """
    filters = {"taxonomy_id": taxonomy_id} if taxonomy_id else {}
    base_qs = (
        ObjectTag.objects
        .unarchived()
        .filter(object_id=object_id, **filters)
        .exclude(taxonomy__enabled=False)  # Exclude if the whole taxonomy is disabled
    )
    if not include_deleted:
        # Exclude if the whole taxonomy was deleted
        base_qs = base_qs.exclude(taxonomy=None)
        # Exclude if just the tag is deleted
        base_qs = base_qs.exclude(tag=None, taxonomy__allow_free_text=False)
    tags = (
        base_qs
        # Preload related objects, including data for the "get_lineage" method on ObjectTag/Tag:
        .select_related("taxonomy", "tag")
        # Sort the tags within each taxonomy in "tree order". See Taxonomy._get_filtered_tags_deep for details on this.
        # tag__lineage is a case-insensitive column storing the full ancestor path, e.g. "Root\tParent\tThis\t".
        # Free-text and deleted tags (tag_id IS NULL) fall back to their cached _value.
        .annotate(sort_key=Coalesce(
            Lower(F("tag__lineage")),
            Lower(Concat(F("_value"), Value("\t"))),
            output_field=models.TextField(),
        ))
        .annotate(taxonomy_name=Coalesce(F("taxonomy__name"), F("_export_id")))
        # Sort first by taxonomy name, then by tag value in tree order:
        .order_by("taxonomy_name", "sort_key")
    )
    return tags


def get_object_tag_counts(object_id_pattern: str, count_implicit=False) -> dict[str, int]:
    """
    Given an object ID, a "starts with" glob pattern like
    "course-v1:foo+bar+baz@*", or a list of "comma,separated,IDs", return a
    dict of matching object IDs and how many tags each object has.

    Deleted tags, disabled taxonomies, and archived object tags (including those
    whose tag or taxonomy is archived) are excluded from the counts, even if
    ObjectTag data about them is present.

    count_implicit: if True, this means to count all ancestor tags (implicit
    tags) of the explict tags that are associated with the object.
    """
    # Note: in the future we may add an option to exclude system taxonomies from the count.
    qs: Any = ObjectTag.objects.unarchived()
    if object_id_pattern.endswith("*"):
        qs = qs.filter(object_id__startswith=object_id_pattern[0:len(object_id_pattern) - 1])
    elif "*" in object_id_pattern:
        raise ValueError("Wildcard matches are only supported if the * is at the end.")
    else:
        qs = qs.filter(object_id__in=object_id_pattern.split(","))
    # Don't include deleted tags or disabled taxonomies:
    qs = qs.exclude(taxonomy_id=None)  # The whole taxonomy was deleted
    qs = qs.exclude(taxonomy__enabled=False)  # The whole taxonomy is disabled
    qs = qs.exclude(tag_id=None, taxonomy__allow_free_text=False)  # The taxonomy exists but the tag is deleted
    if count_implicit:
        # Use tag__lineage to count implicit (ancestor) tags at any depth.
        # Each tag's lineage encodes its full ancestry path, e.g. "root\tchild\tgrandchild\t".
        # Every prefix of that path (at each \t boundary) uniquely identifies one tag in the chain,
        # so collecting prefixes into a set naturally deduplicates shared ancestors across multiple
        # tags on the same object.
        qs = qs.annotate(sort_key=F("tag__lineage")).values("object_id", "sort_key")
        result: dict = {}
        for row in qs:
            object_id = row["object_id"]
            if object_id not in result:
                result[object_id] = {"free_text": 0, "paths": set()}
            sort_key = row["sort_key"]
            if sort_key is None:
                # Free-text tag: no Tag record, so no sort_key
                result[object_id]["free_text"] += 1
            else:
                # Add the sort_key prefix for each ancestor level
                parts = sort_key.rstrip("\t").split("\t")
                for i in range(1, len(parts) + 1):
                    result[object_id]["paths"].add("\t".join(parts[:i]))
        return {
            object_id: data["free_text"] + len(data["paths"])
            for object_id, data in result.items()
        }
    else:
        qs = qs.values("object_id").annotate(num_tags=models.Count("id")).order_by("object_id")
        return {row["object_id"]: row["num_tags"] for row in qs}


def delete_object_tags(object_id: str, *, keep_hidden: bool = False) -> None:
    """
    Delete the ObjectTag entries for a given object.

    Deletion-locked object tags are archived instead of deleted, whatever ``keep_hidden`` is.

    :param object_id: The object whose object tags are deleted.
    :param keep_hidden: If False, all of the object's object tags are deleted, including archived ones and ones whose
        tag or taxonomy is archived. If True, only the object tags that :meth:`ObjectTagQuerySet.unarchived` returns
        are deleted, and the hidden ones are left untouched: they are not deleted, not archived again, and their
        ``deletion_locked`` value is unchanged.
    """
    if not keep_hidden:
        ObjectTag.objects.filter(object_id=object_id).delete()
        return

    pks = list(ObjectTag.objects.filter(object_id=object_id).unarchived().values_list("pk", flat=True))
    # Delete by primary key: the queryset delete locks the rows it selects, and unarchived() joins the tag and taxonomy
    # tables, so deleting the joined queryset would lock shared tag and taxonomy rows on MariaDB.
    ObjectTag.objects.filter(pk__in=pks).delete()


def _check_new_tag_count(
    new_tag_count: int,
    taxonomy: Taxonomy | None,
    object_id: str,
    taxonomy_export_id: str | None = None,
) -> None:
    """
    Checks if the new count of tags for the object is equal or less than OBJECT_MAX_TAGS

    Archived object tags don't count toward the limit.
    """
    # Exclude to avoid counting the tags that are going to be updated
    active_tags = ObjectTag.objects.unarchived().filter(object_id=object_id)
    if taxonomy:
        current_count = active_tags.exclude(taxonomy_id=taxonomy.id).count()
    else:
        current_count = active_tags.exclude(_export_id=taxonomy_export_id).count()

    _check_object_tag_limit(current_count, new_tag_count, object_id)


def _check_object_tag_limit(current_count: int, new_tag_count: int, object_id: str) -> None:
    """
    Raises ValueError if ``new_tag_count`` tags on top of ``current_count`` would exceed OBJECT_MAX_TAGS.
    """
    if current_count + new_tag_count > OBJECT_MAX_TAGS:
        raise ValueError(
            _("Cannot add more than {limit} tags to ({object_id}).").format(object_id=object_id, limit=OBJECT_MAX_TAGS)
        )


def _check_taxonomy_allows_tag_count(taxonomy: Taxonomy, tag_count: int) -> None:
    """
    Raises ValueError if ``tag_count`` tags on one object is more than the taxonomy allows.
    """
    if not taxonomy.allow_multiple and tag_count > 1:
        raise ValueError(_("Taxonomy ({name}) only allows one tag per object.").format(name=taxonomy.name))


def _get_current_tags(
    taxonomy: Taxonomy | None,
    tags: list[str],
    object_id: str,
    taxonomy_export_id: str | None = None,
) -> list[ObjectTag]:
    """
    Returns the current object tags of the related object_id with taxonomy
    """
    if taxonomy:
        _check_taxonomy_allows_tag_count(taxonomy, len(tags))
        current_tags = list(
           ObjectTag.objects.filter(taxonomy=taxonomy, object_id=object_id)
        )
    else:
        current_tags = list(
            ObjectTag.objects.filter(_export_id=taxonomy_export_id, object_id=object_id)
        )
    return current_tags


def _find_existing_object_tag(
    existing_object_tags: list[ObjectTag],
    value: str,
    tag: Tag | None = None,
) -> ObjectTag | None:
    """
    Find the object tag that the object already has for one of the tags that `tag_object()` was asked to apply.

    `tag_object()` receives the full list of tags that an object should have. For each one, this looks through
    ``existing_object_tags`` (which may include archived ones) for the object tag that already links the object to
    that tag or value, so that row is kept and restored instead of a duplicate being created.

    If ``tag`` is given, the existing object tag with that tag is the one. If ``tag`` is ``None``, which is the case
    for free-text, unresolved and no-taxonomy values, it is the existing object tag with no tag whose stored value
    equals ``value``.

    :param existing_object_tags: The object tags that the object already has for this taxonomy, as loaded by
        `tag_object()`.
    :param value: The value being applied.
    :param tag: The Tag being applied, or ``None`` for a free-text or unresolved value.
    :return: The existing object tag to keep, or ``None`` if the caller needs to create a new one.
    """
    if tag is not None:
        for object_tag in existing_object_tags:
            if object_tag.tag_id == tag.id:
                return object_tag
        return None

    object_tags_without_tag = []
    for object_tag in existing_object_tags:
        if object_tag.tag_id is None:
            object_tags_without_tag.append(object_tag)

    object_tags_with_equal_value = _object_tags_with_equal_value(object_tags_without_tag, value)
    if not object_tags_with_equal_value:
        return None
    return _choose_object_tag_to_keep(object_tags_with_equal_value)


def _object_tags_with_equal_value(object_tags_without_tag: list[ObjectTag], value: str) -> list[ObjectTag]:
    """
    Return the given object tags whose stored value equals ``value``.

    Equal means equal as the database sees it, because that is what the unique constraint on the value uses:
    "Alpha" and "alpha" are equal on MySQL but different on SQLite.

    :param object_tags_without_tag: Object tags whose ``tag_id`` is ``None``, so their ``value`` is the stored value.
    :param value: The value to look for.
    """
    object_tags_spelled_identically = []
    for object_tag in object_tags_without_tag:
        if object_tag.value == value:
            object_tags_spelled_identically.append(object_tag)
    if object_tags_spelled_identically:
        # Identical strings are equal under any collation, so there is no need to ask the database.
        return object_tags_spelled_identically

    if not object_tags_without_tag:
        return []

    # Python can't tell which different spellings the database treats as equal, so ask the database. Use the write
    # database, as the saves in `tag_object()` do.
    primary_keys_with_equal_value = set(
        ObjectTag.objects.using(router.db_for_write(ObjectTag))
        .filter(pk__in=[object_tag.pk for object_tag in object_tags_without_tag], _value=value)
        .values_list("pk", flat=True)
    )
    object_tags_with_equal_value = []
    for object_tag in object_tags_without_tag:
        if object_tag.pk in primary_keys_with_equal_value:
            object_tags_with_equal_value.append(object_tag)
    return object_tags_with_equal_value


def _choose_object_tag_to_keep(object_tags_with_equal_value: list[ObjectTag]) -> ObjectTag:
    """
    Choose which of several object tags with equal values to keep.

    Several can only exist when there is no taxonomy, because no unique constraint applies then. A deletion-locked
    object tag is preferred, because it carries learner history. Among the remaining ones, the lowest primary key
    (the oldest) wins.
    """
    deletion_locked_object_tags = []
    for object_tag in object_tags_with_equal_value:
        if object_tag.deletion_locked:
            deletion_locked_object_tags.append(object_tag)

    if deletion_locked_object_tags:
        object_tags_to_choose_from = deletion_locked_object_tags
    else:
        object_tags_to_choose_from = object_tags_with_equal_value
    return min(object_tags_to_choose_from, key=operator.attrgetter("pk"))


def _restore_existing_object_tag(object_tag: ObjectTag, value: str) -> list[str]:
    """
    Bring an existing object tag in line with the value being applied to the object, in memory only.

    Restoring means un-archiving it. An author applying the tag again owns it, so it also stops being marked as
    copied, otherwise Studio couldn't delete it and the next library sync would archive it again.
    ``deletion_locked`` is left as it is.

    :param object_tag: The existing object tag found by :func:`_find_existing_object_tag`.
    :param value: The value being applied. It replaces the stored value, which may be spelled differently or be out
        of date.
    :return: The names of the model fields that were changed, to pass to ``save(update_fields=...)``. Empty if
        nothing needed to change.
    """
    changed_fields: list[str] = []
    if object_tag.archived:
        object_tag.archived = False
        object_tag.is_copied = False
        changed_fields.extend(["archived", "is_copied"])
    if object_tag._value != value:  # pylint: disable=protected-access
        object_tag._value = value  # pylint: disable=protected-access
        changed_fields.append("_value")
    return changed_fields


def _keep_existing_object_tag(
    existing_object_tags: list[ObjectTag],
    object_tags_to_update: list[tuple[ObjectTag, list[str]]],
    value: str,
    tag: Tag | None = None,
) -> bool:
    """
    Keep the existing object tag for a value being applied, if there is one.

    The object tag found is removed from ``existing_object_tags`` (what remains there at the end gets removed from
    the object), restored, and added to ``object_tags_to_update`` if restoring changed any of its fields.

    :return: ``True`` if an existing object tag was kept, ``False`` if the caller needs to create a new one.
    """
    object_tag = _find_existing_object_tag(existing_object_tags, value, tag)
    if object_tag is None:
        return False
    existing_object_tags.remove(object_tag)
    changed_fields = _restore_existing_object_tag(object_tag, value)
    if changed_fields:
        object_tags_to_update.append((object_tag, changed_fields))
    return True


def _save_object_tags(
    object_tags_to_remove: list[ObjectTag],
    object_tags_to_update: list[tuple[ObjectTag, list[str]]],
    object_tags_to_create: list[ObjectTag],
) -> None:
    """
    Apply the changes that :func:`tag_object` worked out, all in one transaction to avoid partial updates.

    :param object_tags_to_remove: Existing object tags to delete, or to archive if they are deletion-locked.
    :param object_tags_to_update: Existing object tags that were changed in memory, each with the names of the
        fields that changed.
    :param object_tags_to_create: New, unsaved object tags.
    """
    with transaction.atomic():
        # Delete or archive the omitted object tags first, so freed values don't collide with the saves below.
        if object_tags_to_remove:
            ObjectTag.objects.filter(pk__in=[object_tag.pk for object_tag in object_tags_to_remove]).delete()

        for object_tag, changed_fields in object_tags_to_update:
            object_tag.full_clean()  # Run validation
            # `tag_object()` read these rows before this transaction started. Saving only the changed columns keeps
            # a `deletion_locked` that was committed in between from being overwritten with the stale value.
            object_tag.save(update_fields=changed_fields)

        for object_tag in object_tags_to_create:
            object_tag.full_clean()  # Run validation
            object_tag.save()


def tag_object(
    object_id: str,
    taxonomy: Taxonomy | None,
    tags: list[str],
    create_invalid: bool = False,
    taxonomy_export_id: str | None = None,
) -> None:
    """
    Replaces the existing ObjectTag entries for the given taxonomy + object_id
    with the given list of tags.

    tags: A list of the values of the tags from this taxonomy to apply.

    Raised Tag.DoesNotExist if the proposed tags are invalid for this taxonomy.
    Preserves existing (valid) tags, adds new (valid) tags, and removes omitted
    (or invalid) tags.
    create_invalid: You can create invalid tags and avoid the previous behavior using.

    taxonomy_export_id: You can create object tags without taxonomy using this param
    and `taxonomy` as None. You need to use the taxonomy.export_id, so you can resync
    this object tag if the taxonomy is created in the future.

    Applying a tag whose object tag is archived restores that same row, and leaves its ``deletion_locked``
    unchanged. Omitted object tags that are deletion-locked are archived instead of deleted. A restored free-text
    value is stored with the newly applied spelling.
    Omitted object tags that are archived, or whose tag or taxonomy is archived (the ones
    :meth:`ObjectTagQuerySet.unarchived` excludes), are left alone, because a caller that builds its list from the
    filtered read paths never saw them.
    """
    if not isinstance(tags, list):
        raise ValueError(_("Tags must be a list, not {type}.").format(type=type(tags).__name__))

    tags = list(dict.fromkeys(tags))  # Remove duplicates preserving order

    if not taxonomy and not taxonomy_export_id:
        raise ValueError("`taxonomy_export_id` can't be None if `taxonomy` is None")

    _check_new_tag_count(len(tags), taxonomy, object_id, taxonomy_export_id)
    existing_object_tags = _get_current_tags(
        taxonomy,
        tags,
        object_id,
        taxonomy_export_id
    )

    object_tags_to_create: list[ObjectTag] = []
    object_tags_to_update: list[tuple[ObjectTag, list[str]]] = []
    if taxonomy and taxonomy.allow_free_text:
        for tag_value in tags:
            if not _keep_existing_object_tag(existing_object_tags, object_tags_to_update, tag_value):
                object_tag = ObjectTag(taxonomy=taxonomy, object_id=object_id, _value=tag_value)
                object_tags_to_create.append(object_tag)
    else:
        # Handle closed taxonomies:
        for tag_value in tags:
            tag = None
            # When export, sometimes, the value has a space at the beginning and end.
            tag_value = tag_value.strip()
            if taxonomy:
                try:
                    tag = taxonomy.tag_for_value(tag_value)  # Will raise Tag.DoesNotExist if the value is invalid.
                except Tag.DoesNotExist as e:
                    if not create_invalid:
                        raise e

            if tag:
                # Tag exists in the taxonomy.
                # If this tag is already applied, its object tag is also brought in line with the tag's current value.
                if not _keep_existing_object_tag(existing_object_tags, object_tags_to_update, tag.value, tag):
                    # We are newly applying this tag:
                    object_tag = ObjectTag(taxonomy=taxonomy, object_id=object_id, tag=tag)
                    object_tags_to_create.append(object_tag)
            elif taxonomy:
                # Tag doesn't exist in the taxonomy and `create_invalid` is True
                if not _keep_existing_object_tag(existing_object_tags, object_tags_to_update, tag_value):
                    object_tag = ObjectTag(taxonomy=taxonomy, object_id=object_id, _value=tag_value)
                    object_tags_to_create.append(object_tag)
            else:
                # Taxonomy is None (also tag doesn't exist)
                kept_existing_object_tag = _keep_existing_object_tag(
                    existing_object_tags, object_tags_to_update, tag_value
                )
                # `taxonomy_export_id` is always set here, since it is verified at the beginning of the function.
                # The check below only tells the type checker that.
                if not kept_existing_object_tag and taxonomy_export_id:
                    object_tag = ObjectTag(
                        taxonomy=None,
                        object_id=object_id,
                        _value=tag_value,
                        _export_id=taxonomy_export_id
                    )
                    object_tags_to_create.append(object_tag)

    # Whatever is left in `existing_object_tags` was not asked for. The read paths hide a row that is archived, or
    # whose tag or taxonomy is archived, so a caller that builds its list from them never saw such a row, and removing
    # it would delete or re-archive something the caller can't see. Only the rows that `unarchived()` returns are
    # removed. A row hidden for another reason, such as a disabled taxonomy, is still removed.
    active_pks = set(
        ObjectTag.objects.filter(pk__in=[object_tag.pk for object_tag in existing_object_tags])
        .unarchived()
        .values_list("pk", flat=True)
    )
    object_tags_to_remove = [object_tag for object_tag in existing_object_tags if object_tag.pk in active_pks]
    _save_object_tags(object_tags_to_remove, object_tags_to_update, object_tags_to_create)


def add_tag_to_object(object_id: str, tag: Tag) -> ObjectTag:
    """
    Adds one existing ``tag`` to ``object_id``, and returns its ObjectTag.

    UNSTABLE: this function is new, so its shape may change without a deprecation cycle.

    Unlike :func:`tag_object`, no other ObjectTag of the object is read for replacement or changed,
    so a concurrent write of another tag on the same object, even one in the same taxonomy, is not
    undone by this call. If ``tag`` is already applied to the object, its existing ObjectTag is
    returned and nothing is written; an archived ObjectTag for the same object and tag is revived in
    place (``archived=False``) and returned, keeping its ``deletion_locked`` flag, because the unique
    constraint allows one row per object and tag and a new row would drop that lock.

    Raises ValueError if the taxonomy of ``tag`` allows only one tag per object and the object
    already has a different tag in it, or if the object already has the maximum number of tags
    (``OBJECT_MAX_TAGS``). Both use the same messages as :func:`tag_object`, and as there, archived
    ObjectTags do not count toward either limit. A revive meets the same limits as a new tag, since
    the object may have been tagged since the archive. Raises ValueError if ``tag`` does not belong
    to a taxonomy. Raises Django's ``ValidationError`` if the new ObjectTag fails ``full_clean()``.

    The checks and the insert are not atomic, so a caller that needs them to hold against concurrent
    writers must serialize those writers itself.
    """
    taxonomy = tag.taxonomy
    if taxonomy is None:
        raise ValueError(_("Tag ({value}) does not belong to a taxonomy.").format(value=tag.value))

    object_tag = ObjectTag.objects.filter(object_id=object_id, taxonomy_id=taxonomy.id, tag_id=tag.id).first()
    if object_tag is not None and not object_tag.archived:
        return object_tag

    # An active row would be the one after the active rows the object already has. Archived rows
    # don't count toward either limit, as in tag_object(), and the row about to be revived is
    # archived, so it never counts against itself.
    active_tags = ObjectTag.objects.unarchived().filter(object_id=object_id)
    if not taxonomy.allow_multiple:
        _check_taxonomy_allows_tag_count(taxonomy, active_tags.filter(taxonomy_id=taxonomy.id).count() + 1)
    _check_object_tag_limit(active_tags.count(), 1, object_id)

    if object_tag is not None:
        object_tag.archived = False
        object_tag.save(update_fields=["archived"])
        return object_tag
    object_tag = ObjectTag(taxonomy=taxonomy, object_id=object_id, tag=tag)
    object_tag.full_clean()
    object_tag.save()
    return object_tag


def add_tag_to_taxonomy(
    taxonomy: Taxonomy,
    tag: str,
    parent_tag_value: str | None = None,
    external_id: str | None = None
) -> Tag:
    """
    Adds a new Tag to provided Taxonomy. If a Tag already exists in the
    Taxonomy, an exception is raised, otherwise the newly created
    Tag is returned.

    If `external_id` is omitted, one is generated from the tag's name.

    Raises ``ValueError`` if the taxonomy is archived, if the value or ``external_id``
    is held by another tag, archived or not, or if the parent is archived or under an
    archived tag.
    """
    new_tag = taxonomy.add_tag(tag, parent_tag_value, external_id)

    # Resync all related ObjectTags after creating new Tag to
    # to ensure any existing ObjectTags with the same value will
    # be linked to the new Tag
    object_tags = taxonomy.objecttag_set.all()
    resync_object_tags(object_tags)

    return new_tag


def update_tag_in_taxonomy(taxonomy: Taxonomy, tag: str, new_value: str):
    """
    Update a Tag that belongs to a Taxonomy. The related ObjectTags are
    updated accordingly.

    Currently only supports updating the Tag value.

    Raises ValueError if the taxonomy is archived, or if the tag is archived.
    """
    updated_tag = taxonomy.update_tag(tag, new_value)

    # Resync all related ObjectTags to update to the new Tag value
    object_tags = taxonomy.objecttag_set.all()
    resync_object_tags(object_tags)

    return updated_tag


def delete_tags_from_taxonomy(
    taxonomy: Taxonomy,
    tags: list[str],
    with_subtags: bool
):
    """
    Delete Tags that belong to a Taxonomy. With `with_subtags`, their sub-tags are deleted as well.

    A tag is archived instead if it, or any tag beneath it, has a deletion-locked association (``ObjectTag``).

    :param taxonomy: The taxonomy the tags belong to.
    :param tags: The values of the tags to delete.
    :param with_subtags: Whether the tags' sub-tags may be deleted with them.
    :raises ValueError: If the taxonomy is free-text or read-only, if a value is not a tag of this taxonomy, or if
        a tag has unarchived sub-tags, at any depth, and `with_subtags` is not `True`.
    :raises ConcurrentDeleteError: If tags or deletion locks kept being added beneath these tags on every attempt.
    :raises ProtectedError: If none of the tags is archived by the rule above but another model still protects one
        of them from deletion.
    :raises IntegrityError: If the database refused every attempt to delete the tags.
    """
    taxonomy.delete_tags(tags, with_subtags)


def delete_taxonomy(taxonomy: Taxonomy) -> None:
    """
    Delete the taxonomy, or archive it if any of its associations (``ObjectTag``) is deletion-locked.

    An archived taxonomy stays in the database, and ``taxonomy.archived`` is set to match on the instance passed
    in. Archiving does not set ``archived`` on the taxonomy's tags. When the taxonomy is deleted, the instance
    passed in keeps its primary key, unlike after ``taxonomy.delete()``. Calling ``taxonomy.delete()`` directly
    skips this rule.

    Archiving sends :data:`~openedx_tagging.signals.taxonomies_archived` inside the transaction that archives, and
    only if the taxonomy was not already archived.

    :param taxonomy: The taxonomy to delete or archive.
    :raises ProtectedError: If the taxonomy has no deletion-locked association but another model still protects
        one of its tags from deletion.
    :raises IntegrityError: If the database refused every attempt to delete the taxonomy.
    :raises ConcurrentDeleteError: If, on every attempt, a deletion-locked association lost its taxonomy while
        this taxonomy was being deleted.
    """
    taxonomy_id = taxonomy.pk
    # This first check only saves work when the taxonomy is already known to be locked. The check that decides
    # is the one each delete attempt makes.
    write_alias = router.db_for_write(Taxonomy)
    if not is_taxonomy_locked_for_deletion(taxonomy_id):
        if _retry_on_concurrent_write(partial(_delete_taxonomy_unless_locked, taxonomy_id), using=write_alias):
            return
    with transaction.atomic(using=write_alias):
        changed = Taxonomy.objects.using(write_alias).filter(pk=taxonomy_id, archived=False).update(archived=True)
        if changed:
            taxonomies_archived.send(sender=Taxonomy, taxonomy_ids=[taxonomy_id], using=write_alias)
    taxonomy.archived = True


def _delete_taxonomy_unless_locked(taxonomy_id: int) -> bool:
    """
    Make one attempt to delete the taxonomy. Return False, deleting nothing, if it has a deletion-locked association.

    Needs a transaction. The check after the delete catches an association that another transaction
    deletion-locked, or added and deletion-locked, after the first read. It raises so that this attempt is
    rolled back and made again. The check looks at every deletion-locked association without a taxonomy, so one
    that never belonged to this taxonomy and reaches that state during the delete also causes another attempt.
    """
    if ObjectTag.objects.filter(taxonomy_id=taxonomy_id, deletion_locked=True).exists():
        return False
    # Deleting a taxonomy clears the taxonomy of its associations. A deletion-locked association that has no
    # taxonomy after the delete, and was not in that state before it, may be one this delete orphaned. It may also
    # be unrelated to this taxonomy, in which case the next attempt goes through.
    orphaned = ObjectTag.objects.filter(deletion_locked=True, taxonomy__isnull=True)
    orphaned_before = set(orphaned.values_list("pk", flat=True))
    # The taxonomy is deleted by id, since a retry can't reuse an instance whose delete() already ran.
    Taxonomy.objects.filter(pk=taxonomy_id).delete()
    if orphaned.exclude(pk__in=orphaned_before).exists():
        raise _ConcurrentWrite("A tag association was deletion-locked while its taxonomy was being deleted.")
    return True


def copy_tags(source_object_id: str, dest_object_id: str):
    """
    Copy all tags from one object to another.

    This keeps all not-copied tags and removes the previously copied tags of the dest object that the source
    no longer has. Copied tags that the source still has are updated in place, so they keep their rows.
    If there are not-copied tags that also are in 'source_object_id',
    then they become copied.

    Locked copied tags (``deletion_locked=True``) are archived instead of deleted. An archived destination
    tag that the source still has is restored, and takes the source's spelling of the value.

    Copied tags in a disabled taxonomy are left as they are, because the source hides that taxonomy's tags.
    """
    # A disabled taxonomy's tags are hidden from the source read below, which does not mean the source dropped them,
    # so its copies are left as they are.
    copied_tags = ObjectTag.objects.filter(object_id=dest_object_id, is_copied=True).exclude(taxonomy__enabled=False)

    with transaction.atomic():
        source_object_tags = list(get_object_tags(source_object_id))

        if source_object_tags:
            # A destination tag matches a source tag the same way the update_or_create() below looks it up: by tag
            # id for closed tags, and by value for free-text ones. The database does the matching, so that value
            # comparisons follow the column's collation, as the lookups below do.
            matches_a_source_tag = Q()
            for source_object_tag in source_object_tags:
                if source_object_tag.tag_id:
                    matches_a_source_tag |= Q(
                        taxonomy_id=source_object_tag.taxonomy_id, tag_id=source_object_tag.tag_id
                    )
                else:
                    matches_a_source_tag |= Q(
                        taxonomy_id=source_object_tag.taxonomy_id, _value=source_object_tag.value
                    )
            dropped_object_tag_ids = list(copied_tags.exclude(matches_a_source_tag).values_list("pk", flat=True))
        else:
            dropped_object_tag_ids = list(copied_tags.values_list("pk", flat=True))
        if dropped_object_tag_ids:
            # Through the queryset's delete(), which archives locked tags instead of deleting them.
            ObjectTag.objects.filter(pk__in=dropped_object_tag_ids).delete()

        # Copy an create object_tags in destination
        for source_object_tag in source_object_tags:
            destination_lookup: dict[str, Any] = {
                "object_id": dest_object_id,
                "taxonomy_id": cast(int, source_object_tag.taxonomy_id),
            }
            if source_object_tag.tag_id:
                # Match closed tags by id, since the destination's cached _value can be stale.
                destination_lookup["tag_id"] = source_object_tag.tag_id
            else:
                # Free-text object tags all have a null tag_id, so match on the value, as the unique constraint does.
                destination_lookup["_value"] = source_object_tag.value
            ObjectTag.objects.update_or_create(
                **destination_lookup,
                defaults={"is_copied": True, "archived": False, "_value": source_object_tag.value},
                # `_export_id` is only set automatically when the object tag is created, and `_value` is only set
                # from the tag then too. That is why `_value` is also in the defaults, to update an existing one.
            )


def unmark_copied_tags(object_id: str) -> None:
    """
    Update copied object tags on the given object to mark them as "not copied".
    """
    ObjectTag.objects.filter(object_id=object_id).update(is_copied=False)


def _extract_object_tag_ids(object_tag_ids: int | Iterable[int]) -> set[int]:
    """
    Return ``object_tag_ids`` as a set, raising TypeError for anything that is not an int or an iterable of ints.
    """
    if isinstance(object_tag_ids, (str, bytes)) or not isinstance(object_tag_ids, (int, Iterable)):
        raise TypeError(f"ObjectTag ids must be ints, got {object_tag_ids!r} ({type(object_tag_ids).__name__})")
    ids = {object_tag_ids} if isinstance(object_tag_ids, int) else set(object_tag_ids)
    for object_tag_id in ids:
        if isinstance(object_tag_id, bool) or not isinstance(object_tag_id, int):
            raise TypeError(f"ObjectTag ids must be ints, got {object_tag_id!r} ({type(object_tag_id).__name__})")
    return ids


def _lost_tag_or_taxonomy(before: ObjectTag, after: ObjectTag) -> bool:
    """
    Return whether an ObjectTag had a tag or a taxonomy in ``before`` that it no longer has in ``after``.

    A tag or taxonomy that was missing and has been filled in is a repair, so it doesn't count.
    """
    lost_tag = before.tag_id is not None and after.tag_id is None
    lost_taxonomy = before.taxonomy_id is not None and after.taxonomy_id is None
    return lost_tag or lost_taxonomy


def lock_object_tags_for_deletion(object_tag_ids: int | Iterable[int], *, using: str | None = None) -> None:
    """
    Lock the given ObjectTags against deletion.

    A locked object tag is archived rather than deleted, because something outside the tagging app depends on it.
    Locks are permanent; there is no unlock.

    Call this before writing rows that reference the object tag (for example a learner status), in the same
    transaction, so that a concurrent delete and this lock take their row locks in the same order and cannot
    deadlock.

    :param object_tag_ids: One ObjectTag primary key, or an iterable of them.
    :param using: The alias of the database to read and write on, for a caller reacting to a write made there. By
        default Django's router chooses.
    :raises TypeError: If any id is not an int. Strings, bytes, and bools are not coerced.
    :raises ObjectTagDoesNotExist: If any id does not resolve to an ObjectTag, or if an ObjectTag, its tag, or its
        taxonomy is deleted while this function runs. Nothing is locked in that case.
    """
    ids = _extract_object_tag_ids(object_tag_ids)
    if not ids:
        return
    object_tags = ObjectTag.objects.using(using)
    before = object_tags.only("deletion_locked", "tag", "taxonomy").in_bulk(ids)
    missing = ids - before.keys()
    if missing:
        raise ObjectTag.DoesNotExist(f"ObjectTag ids not found: {sorted(missing)}")
    unlocked = [pk for pk, object_tag in before.items() if not object_tag.deletion_locked]
    if unlocked:
        with transaction.atomic(using=using):
            # This update waits for a concurrent delete of the row, its tag, or its taxonomy to finish, so what
            # that delete did is read afterwards. Another caller locking the row first is not an error.
            object_tags.filter(pk__in=unlocked, deletion_locked=False).update(deletion_locked=True)
            after = object_tags.only("tag", "taxonomy").in_bulk(unlocked)

            deleted = set(unlocked) - after.keys()
            if deleted:
                raise ObjectTag.DoesNotExist(f"ObjectTag ids deleted before they could be locked: {sorted(deleted)}")
            orphaned = [pk for pk in unlocked if _lost_tag_or_taxonomy(before[pk], after[pk])]
            if orphaned:
                raise ObjectTag.DoesNotExist(
                    f"ObjectTag ids whose tag or taxonomy was deleted before they could be locked: {sorted(orphaned)}"
                )


def get_tags_locked_for_deletion(tag_ids: Iterable[int]) -> set[int]:
    """
    Return the subset of ``tag_ids`` that have at least one deletion-locked association.

    Archived associations count: a locked association keeps protecting its tag after it is archived.

    :param tag_ids: The tag ids to check. Unknown ids are omitted from the result.
    """
    ids = set(tag_ids)
    locked = ObjectTag.objects.filter(tag_id__in=ids, deletion_locked=True)
    return set(locked.values_list("tag_id", flat=True).distinct())


def is_taxonomy_locked_for_deletion(taxonomy_id: int | None) -> bool:
    """
    Return whether any association in the taxonomy is deletion-locked.

    Archived associations count: a locked association keeps protecting its taxonomy after it is archived.

    :param taxonomy_id: The taxonomy id to check. ``None`` returns ``False``, so that associations left behind by a
        deleted taxonomy, whose ``taxonomy_id`` is NULL, are not mistaken for a locked one.
    """
    if taxonomy_id is None:
        return False
    return ObjectTag.objects.filter(taxonomy_id=taxonomy_id, deletion_locked=True).exists()


def add_usage_counts(taxonomy: Taxonomy, tag_data: TagDataQuerySet) -> TagDataQuerySet:
    """
    Add usage counts to the query result.

    Not a simple raw count of each tags usage. A tag can be directly
    applied to an object, which can be a course, library, module,
    or something else.

    A tag can also be indirectly applied when some of its children
    are applied to an object, it is considered automatically applied.
    So, if the tags "Chemistry" and "Physics" are applied once
    each to different objects, their parent tag "Natural Science" is
    considered indirectly applied to 2 objects.

    Deduplication: A tag can only be applied to a single object once.
    So if two child tags are applied to the same object, e.g.
    "Chemistry" and "Physics" are applied to the same course, the
    parent tag, "Natural Science" is only applied to it once,
    because no tag can be applied to the same object twice.

    Archived object tags, and those whose tag or taxonomy is archived, are not counted.

    For performance reasons, we call this function with the list result of the
    QuerySet so we can then add the counts in-memory rather than annotate to a
    QuerySet which would require a very expensive annotation to join the
    in-memory data to the original QuerySet.
    """

    object_tags = taxonomy.objecttag_set.unarchived().values_list("object_id", "tag__lineage")
    tag_counts: Counter[str] = Counter()
    object_tag_lineage_seen: defaultdict[str, set] = defaultdict(set)

    for object_id, tag_lineage in object_tags:
        # split the lineages to get a dict of {tag.value: [lineages]}
        lineage_tags = list(tag_lineage.split('\t')) if tag_lineage else []
        # de-duplicate based on if the lineage is already 'seen' per object
        unseen_tags = [t for t in lineage_tags if t not in object_tag_lineage_seen[object_id]]

        tag_counts.update(unseen_tags)
        object_tag_lineage_seen[object_id].update(unseen_tags)

    # In-memory 'annotation'; this is faster than using annotate() on the QuerySet.
    for row in tag_data:
        row["usage_count"] = tag_counts.get(row["value"], 0)

    return tag_data
