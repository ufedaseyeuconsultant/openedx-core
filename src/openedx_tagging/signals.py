"""
Signals sent by the tagging app.

UNSTABLE: these signals are new, so their names and payloads may change without a deprecation cycle.

Each signal is sent with ``send()`` as the last step of the transaction that archived the rows, so a receiver
runs inside that transaction. The contract is the same for all three:

- A receiver exception propagates to the caller and rolls back the archive.
- A receiver whose effects are not transactional, such as enqueuing a Celery task or emitting an event, must defer
  them with ``transaction.on_commit()``.
- A receiver may be sent ids that it has already handled, so receivers must be idempotent. For ``tags_archived``,
  two concurrent requests that archive the same tag can each send its id. The ids can also arrive again when
  ``Taxonomy.delete_tags()`` makes another attempt, which it does when a receiver raises ``IntegrityError`` from
  inside its retry window: that attempt is rolled back and the whole delete runs and sends again.
- ``sender`` is the base model class (``ObjectTag``, ``Tag`` or ``Taxonomy``), never a subclass.
- A signal is never sent with an empty id list, and the ids are only of rows that this operation read as
  unarchived and set to archived.
- ``using`` is the alias of the database the archive was written on, as in Django's model signals. A receiver
  must run all of its queries on that alias, because the database router may send reads somewhere else, such as a
  replica that has not yet seen the archive.
"""
from django.dispatch import Signal

#: Sent when deletion-locked ``ObjectTag`` rows are archived instead of deleted.
#:
#: Keyword arguments: ``object_tag_ids`` (``list[int]``), the primary keys of the newly archived object tags, and
#: ``using`` (``str``), the database alias. ``sender`` is ``ObjectTag``.
object_tags_archived = Signal()

#: Sent when ``Taxonomy.delete_tags()`` archives tags instead of deleting them.
#:
#: Keyword arguments: ``tag_ids`` (``list[int]``), the primary keys of the newly archived tags, in ascending order,
#: and ``using`` (``str``), the database alias. ``sender`` is ``Tag``.
tags_archived = Signal()

#: Sent when ``delete_taxonomy()`` archives a taxonomy instead of deleting it.
#:
#: Keyword arguments: ``taxonomy_ids`` (``list[int]``), a list holding the primary key of the newly archived
#: taxonomy, and ``using`` (``str``), the database alias. ``sender`` is ``Taxonomy``.
taxonomies_archived = Signal()
