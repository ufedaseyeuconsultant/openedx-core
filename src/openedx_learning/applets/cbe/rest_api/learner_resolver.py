"""
Chooses whose competency statuses a learner-facing request may read.

The choice depends on the deployment (for example, the LMS lets course staff read an enrolled learner's data and
supports masquerading), so it is delegated to a callable that a host can replace.
"""
from __future__ import annotations

from typing import cast

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.http import Http404
from django.utils.module_loading import import_string
from opaque_keys.edx.keys import CourseKey
from rest_framework.request import Request


def default_learner_resolver(request: Request, course_key: CourseKey, learner_id: int | None) -> AbstractBaseUser:
    """
    Return the user whose statuses the request may read. This is the built-in resolver.

    UNSTABLE: the contract may change without a deprecation cycle while ADR 0008 is Proposed.

    A host replaces it by setting the top-level Django setting ``OPENEDX_LEARNING_LEARNER_RESOLVER`` to the dotted
    path of a callable ``resolver(request, course_key, learner_id) -> User``, where ``request`` is the
    authenticated DRF ``Request``, ``course_key`` is the plain ``CourseKey`` of the course run, and ``learner_id``
    is an ``int`` or ``None`` when the client did not name a learner. See
    ``docs/openedx_learning/decisions/0008-learner-resolver.rst`` for the rest of the contract.

    A resolver refuses with one of two exceptions, and visibility is checked first:

    * ``django.http.Http404`` when the named learner does not exist or the requester may not see them, whatever
      the requester's standing in the course run. Any message is discarded by the view, so the two cases give
      identical responses.
    * ``rest_framework.exceptions.PermissionDenied`` only about a learner the requester may see, which always
      includes the requester, when the requester may not read learner data for the course run.

    A resolver that looks the user up must turn ``User.DoesNotExist`` into ``Http404``: Django REST framework
    renders ``DoesNotExist`` as a 500.

    This default returns ``request.user`` when ``learner_id`` is ``None`` or the requester's own id, and raises
    ``Http404`` for any other id without a database lookup.
    """
    del course_key  # Part of the resolver contract; the default does not need it.
    if learner_id is None or learner_id == request.user.pk:
        # The view's IsAuthenticated permission has already run, so this is never an AnonymousUser.
        return cast(AbstractBaseUser, request.user)
    raise Http404


def resolve_learner(request: Request, course_key: CourseKey, learner_id: int | None) -> AbstractBaseUser:
    """
    Return the user whose statuses the request may read, as decided by the configured resolver.

    UNSTABLE: see :func:`default_learner_resolver`, which documents the contract.

    Loads the callable at ``settings.OPENEDX_LEARNING_LEARNER_RESOLVER`` on every call, or uses
    :func:`default_learner_resolver` when the setting is absent.
    """
    path = getattr(settings, "OPENEDX_LEARNING_LEARNER_RESOLVER", None)
    resolver = import_string(path) if path else default_learner_resolver
    return resolver(request, course_key, learner_id)
