.. _openedx-learning-adr-0008:

8. Learner Resolver: A Host-Defined Answer to "Whose Data May This Request Read"
=================================================================================

Status
------

Proposed

Context
-------

Some learner-facing routes of the CBE applet return data about one learner, for example that learner's competency
statuses in a course run. Such a route accepts a ``learner_id`` query parameter to name the learner, or no
parameter to ask about the requester. The criteria route returns data about a course run, not about a learner, and
is gated by the ``rules`` permission ``view_course_competency_criteria``. Whether a requester may read another
learner's data depends on the deployment, and the host decides it. The LMS is intended to admit any existing user
that a course run's staff name, because a learner's statuses are global and not tied to one course run. A staff
member may also masquerade as a learner, and a requester who has no right to see a learner must get exactly the
same response as one who names a user that does not exist.
That identical response is an acceptance criterion: a distinguishable answer would let a caller test which user ids
exist.

The permission mechanism already in the app, a ``rules`` permission checked against the course run (see
``view_course_competency_criteria``), cannot express this. It sees the requester and the course, not the learner.
On the LMS it would also answer 403 to a member of one course run's staff who names a learner in another run, where
the criterion requires the same 404 that an unknown id gets.

Several learner routes will need the same decision, and ``openedx-platform`` has to build its implementation against
a documented contract. The decision is therefore recorded once.

Decision
--------

1. The learner routes that name a learner with a ``learner_id`` query parameter (the competency statuses route,
   and later routes that adopt the resolver) ask a single callable, the **learner resolver**, whose dotted path is
   the top-level Django setting ``OPENEDX_LEARNING_LEARNER_RESOLVER``. When the setting is absent, a built-in
   default is used.

2. The signature is ``resolver(request, course_key, learner_id) -> User``:

   - ``request`` is the Django REST framework ``Request``. ``request.user`` is already authenticated.
   - ``course_key`` is the plain ``CourseKey`` of the course run, with branch and version stripped.
   - ``learner_id`` is an ``int``, or ``None`` when the client did not name a learner. The routes turn a value that
     is not a canonical positive decimal integer of at most 18 ASCII digits (no sign, spaces, leading zeros,
     underscores, or non-ASCII digits) into a 404 before the resolver is called, so no resolver parses strings.
   - The return value is the user whose data the request may read.

3. A resolver refuses in one of two ways, and visibility is checked first:

   - It raises ``django.http.Http404`` when the named learner does not exist or the requester may not see them,
     whatever the requester's standing in the course run. This includes a member of the course run's staff who
     names a learner they may not see. The view catches ``Http404`` and Django REST framework's ``NotFound`` from
     the resolver and raises a bare ``Http404``, so any message is discarded by the view. Django REST framework
     copies an exception's arguments into the response body, and a message would make "not visible"
     distinguishable from "no such user".
   - It raises ``rest_framework.exceptions.PermissionDenied`` only about a learner the requester may see, which
     always includes the requester, when the requester may not read learner data for the course run. This is the
     narrowest choice that every common exception handler renders as a 403.

   A resolver that looks the user up must turn ``User.DoesNotExist`` into ``Http404``, because Django REST
   framework renders ``DoesNotExist`` as a 500 error.

4. The built-in default admits only the requester. It returns ``request.user`` when ``learner_id`` is ``None`` or
   the requester's own id. For any other id it raises a bare ``Http404`` without a database lookup, so an unknown id
   and another user's id produce identical responses and no lookup exists whose timing could reveal which ids are
   real. A signed-in user may always read their own data, so this default is safe without host configuration. The
   default ignores course access: any signed-in user may read their own statuses for any course key, because the
   response then holds only that user's own data.

5. The setting is read and the callable imported on every call. Skipping a cache lets tests change the setting with
   ``override_settings``, and after the first call ``import_string`` does no import work.

6. The setting is top-level and not a key of the ``OPENEDX_LEARNING`` dict. The LMS applies each top-level key of its
   YAML configuration to the Django settings, so an operator who sets ``OPENEDX_LEARNING`` replaces the whole dict
   and would silently drop a resolver path stored inside it.

Alternatives Considered
-----------------------

**A** ``rules`` **permission.** The existing permission style is familiar to hosts. It is rejected because its
object is the course run, so it cannot depend on the learner, and because a check before the resolver would turn
the required 404 into a 403 on the LMS. A check after the resolver adds nothing, since the resolver already makes
the whole decision. Keeping one seam responsible for one decision avoids two mechanisms that can disagree.

**An openedx-filters filter.** A filter pipeline lets several plugins act on the same event. It is rejected because
its steps pass a dict of keyword arguments through the pipeline rather than returning a single user, and an
unconfigured pipeline returns its input unchanged, so the self-only default would still have to live in the calling
code. Pipelines are also configured in the separate ``OPEN_EDX_FILTERS_CONFIG`` structure, which adds a second
place to look for something a single dotted path expresses.

**A key inside the** ``OPENEDX_LEARNING`` **dict.** Grouping the setting with the app's other configuration is
tidier. It is rejected for the reason in decision 6: a host's YAML loader replaces that dict whole.

Consequences
------------

- A host that needs staff access, masquerading, or enrollment checks writes one function and points the setting at
  it. ``openedx-platform`` can wrap its existing learner lookup, which already raises a bare ``Http404``.
- Without configuration, the routes that use the resolver serve a requester only their own data, and answer 404 for
  any other ``learner_id``.
- Later learner routes that name a learner reuse the same seam and inherit its refusal behavior.
- The resolver contract is UNSTABLE while this ADR is Proposed and the routes that use the resolver are UNSTABLE, so
  it may change without a deprecation cycle. Once this ADR is Accepted and those routes are stabilized, changing the
  signature or exception semantics follows the DEPR process, as for other public APIs.
- A host that restricts ``view_course_competency_criteria`` must also set a resolver that applies the same
  restriction. The default resolver ignores course access, so without one the criteria route and the learner routes
  disagree on who may read a course run.
- A host resolver that admits course staff lets them read where else the learner demonstrated a competency, because
  ``demonstrated_in_course`` names another course run, possibly one that belongs to another organization. The ticket
  records that cross-organization read as a known limitation, in the "known limitations" section of
  openedx/openedx-core#738. The response shows that run's course key and title. The LMS path for staff also reads any
  existing user id, whether or not that user is enrolled.
