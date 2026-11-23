.. _openedx-tagging-adr-0014:

14. Signals for archived associations, tags, and taxonomies
============================================================

Status
------

Proposed

Context
-------

Tagging archives a row instead of deleting it when another app has locked it for deletion, as CBE does when a
learner status depends on an association. An ``ObjectTag`` with
``deletion_locked=True`` is archived when a delete is requested, and so is a ``Tag`` or a ``Taxonomy`` that has such an
association beneath it. Archiving writes the row and removes nothing, so no Django ``post_delete`` signal fires and
nothing cascades. An app that keeps its own rows alongside tagging records, such as the competency criteria of the CBE
applet in ``openedx_learning``, therefore has no way to learn that the records it depends on were archived.

The signal is sent inside the transaction that archived the rows, so a receiver chooses its coupling: one whose
reaction must be undone with a rolled-back archive reacts in the same transaction and lets its exceptions propagate,
while one that must never block or roll back the archive defers its reaction to
``transaction.on_commit(using=using)``, as the CBE receivers do. The reaction also has to read the state it reacts
to, so it must run its queries on the database alias that the archive was written on, because a database router may
send reads to a replica that has not yet seen the write.

Decision
--------

``openedx_tagging.signals`` defines three Django signals:

- ``object_tags_archived`` is sent with ``sender=ObjectTag`` by ``ObjectTagQuerySet.delete()`` and carries
  ``object_tag_ids``.
- ``tags_archived`` is sent with ``sender=Tag`` by ``Taxonomy.delete_tags()`` and carries ``tag_ids``.
- ``taxonomies_archived`` is sent with ``sender=Taxonomy`` by ``delete_taxonomy()`` and carries ``taxonomy_ids``.

Each signal follows one contract:

- Every signal also carries ``using``, the alias of the database the archive was written on. A receiver runs all of
  its queries and writes on that alias.
- ``send()`` is the last step of the transaction that archived the rows, so receivers run inside that transaction.
  An exception in a receiver that reacts in the transaction propagates to the caller and rolls the archive back. A
  receiver whose effects are not transactional, such as enqueuing a Celery task or emitting an event, or whose failure
  must never roll the archive back, defers its reaction with ``transaction.on_commit()``.
- The ids are only those of rows that the operation read as unarchived and set to archived. A signal is never sent
  with an empty list, and the sender is always the base model class, never a subclass such as ``CompetencyTaxonomy``.
- Receivers are idempotent. For ``tags_archived``, two concurrent requests that archive the same tag can each send
  its id, and ``Taxonomy.delete_tags()`` runs its whole attempt, including the send, again when a receiver raises
  ``IntegrityError``.

Django signals are used instead of ``openedx-events``, for the reasons under Rejected Alternatives. A receiver that
wants to publish an event of its own does so from ``transaction.on_commit()``.

The signals are marked UNSTABLE in ``openedx_tagging/signals.py``. Their names and payloads may change without a
deprecation cycle until the first consumers have settled the shape.

Consequences
------------

- ``openedx_tagging`` stays unaware of its consumers. The CBE applet connects its own receivers in
  ``openedx_learning.applets.cbe.signal_handlers``, which archive the criteria and criteria groups of an archived
  association, tag, or taxonomy.
- A receiver that reacts in the archive's transaction slows or fails the request that archives. The CBE receivers
  defer their cleanup to ``transaction.on_commit()``, which still runs in the request after the commit, so it slows
  the request but cannot fail or roll back the archive. A taxonomy archive runs every receiver in the request.
- A receiver can be called with ids it has already handled, so each one has to tolerate that.

Rejected Alternatives
---------------------

``openedx-events``
~~~~~~~~~~~~~~~~~~

An ``openedx-events`` signal is a Django signal whose payload is a versioned attrs data class, designed so that the
event bus can carry it to consumers in other processes. Three things rule it out for this contract:

- Outside ``settings.DEBUG``, ``send_event()`` dispatches through ``send_robust()`` by default, which logs a
  receiver's exception instead of raising it, so a receiver that chooses to react in the transaction could not
  roll the archive back by raising.
- The payload carries no database alias, and an event bus consumer in another process can share neither the
  archiving transaction nor its database connection.
- This repository sends its events after the commit, from ``transaction.on_commit()`` or from a Celery task, as the
  publishing API in ``openedx_content`` and ``openedx_tagging.tasks`` do, so that a consumer learns of a change only
  once it is committed.

Calling the consumer directly from the tagging app
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``openedx_tagging`` would have to import the CBE applet, which reverses the dependency direction recorded in
`ADR 0013 <0013-competency-taxonomy-detection.rst>`_ and fails for an install that runs tagging without CBE.
