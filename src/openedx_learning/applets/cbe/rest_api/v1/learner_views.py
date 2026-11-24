"""
Views for the CBE REST API, v1, for learners.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, cast

from django.http import Http404
from django.utils.translation import gettext_lazy as _
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication  # type: ignore[import]
from edx_rest_framework_extensions.auth.session.authentication import (  # type: ignore[import]
    SessionAuthenticationAllowInactiveUser,
)
from opaque_keys import InvalidKeyError
from rest_framework import generics
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ...api import get_course_competency_criteria, get_learner_competency_statuses
from ...data import LearnerCompetency, LearnerCourseCriteria
from ..learner_resolver import resolve_learner
from ..paginators import LearnerCourseCriteriaPagination
from .learner_serializers import LearnerCompetencyStatusesSerializer, LearnerCourseCriteriaSerializer
from .serializers import parse_plain_course_key


class LearnerCourseCriteriaView(generics.GenericAPIView):
    """
    GET-only. Return the competency criteria of one course run, for the learner Progress tab.

    ``GET api/cbe/v1/courses/<course_key>/criteria/``

    UNSTABLE, and what is left out of the lists: see ``get_course_competency_criteria``.

    The caller must have ``openedx_learning.view_course_competency_criteria``, checked against the plain
    course key. Checks run in this order: sign-in (401), course key (400), permission (403).

    **Example Response**

    .. code-block:: json

        {
            "taxonomies": [
                {"id": 3, "name": "Problem Solving Skills"}
            ],
            "competencies": [
                {"id": 12, "name": "Root Cause Analysis", "external_id": "RCA-1",
                 "lineage": ["Problem Solving", "Problem Definition", "Root Cause Analysis"], "taxonomy_id": 3}
            ],
            "groups": [
                {"id": 40, "tag_id": 12, "parent_id": null, "name": "Root Cause Analysis",
                 "logic_operator": "AND", "ordering": 0},
                {"id": 41, "tag_id": 12, "parent_id": 40, "name": "Python 100",
                 "logic_operator": "OR", "ordering": 0},
                {"id": 42, "tag_id": 12, "parent_id": 41, "name": "Week 1 quiz",
                 "logic_operator": null, "ordering": 0}
            ],
            "criteria": [
                {"id": 7, "group_id": 42, "object_id": "block-v1:Org1+Python100+Fall2026+type@sequential+block@s1",
                 "rule_type": "Grade", "rule_payload": {"op": "gte", "value": 0.75, "scale": "percent"}}
            ]
        }

    The response is paginated only when ``page`` or ``page_size`` is in the query string. A page holds up to
    ``page_size`` competencies with their taxonomies, groups and criteria, and adds these keys to the body:

    .. code-block:: json

        {
            "count": 3,
            "num_pages": 2,
            "current_page": 1,
            "next": "https://example.com/api/cbe/v1/courses/course-v1:Org1+Python100+Fall2026/criteria/?page=2",
            "previous": null
        }

    A page past the end is a 404.
    """

    authentication_classes = (JwtAuthentication, SessionAuthenticationAllowInactiveUser)
    permission_classes = [IsAuthenticated]
    pagination_class = LearnerCourseCriteriaPagination
    http_method_names = ["get"]

    def get(self, request, course_key):
        """Return the course run's criteria, after the key is parsed and the permission checked."""
        try:
            plain_course_key = parse_plain_course_key(course_key)
        except InvalidKeyError as exc:
            raise DRFValidationError(
                {"course_key": [_("'%(value)s' is not a valid course run key.") % {"value": course_key}]}
            ) from exc
        if not request.user.has_perm("openedx_learning.view_course_competency_criteria", plain_course_key):
            raise PermissionDenied()

        result = get_course_competency_criteria(plain_course_key)
        if "page" in request.query_params or "page_size" in request.query_params:
            return self._get_paginated_response(request, result)
        return Response(LearnerCourseCriteriaSerializer(result).data)

    def _get_paginated_response(self, request, result: LearnerCourseCriteria) -> Response:
        """Return a page of ``result``'s competencies, with their taxonomies, groups and criteria."""
        paginator = cast(LearnerCourseCriteriaPagination, self.paginator)
        # paginate_queryset is typed for querysets, but it pages any sequence and returns a list.
        competencies = cast(
            list[LearnerCompetency], paginator.paginate_queryset(cast(Any, result.competencies), request, view=self),
        )
        competency_ids = {competency.id for competency in competencies}
        taxonomy_ids = {competency.taxonomy_id for competency in competencies}
        groups = [group for group in result.groups if group.tag_id in competency_ids]
        group_ids = {group.id for group in groups}
        page_result = replace(
            result,
            taxonomies=[taxonomy for taxonomy in result.taxonomies if taxonomy.id in taxonomy_ids],
            competencies=competencies,
            groups=groups,
            criteria=[criterion for criterion in result.criteria if criterion.group_id in group_ids],
        )
        page = paginator.page
        return Response({
            **LearnerCourseCriteriaSerializer(page_result).data,
            "count": page.paginator.count,
            "num_pages": page.paginator.num_pages,
            "current_page": page.number,
            "next": paginator.get_next_link(),
            "previous": paginator.get_previous_link(),
        })


# 18 digits always fit a signed 64-bit id, and Django answers an out-of-range exact lookup with no rows,
# so an accepted value can never make a host's lookup error.
_LEARNER_ID = re.compile(r"[1-9][0-9]{0,17}")


class LearnerCompetencyStatusesView(APIView):
    """
    GET-only. Return one learner's competency and criterion statuses for a course run, for the Progress tab.

    ``GET api/cbe/v1/courses/<course_key>/competency-statuses/``

    UNSTABLE, and what each list holds: see ``get_learner_competency_statuses``.

    The optional query parameter ``learner_id`` names the learner, as a positive integer user id of at most 18
    ASCII digits, without a sign, spaces, underscores or leading zeros. The learner resolver decides whose
    statuses a request may read (see
    :func:`openedx_learning.applets.cbe.rest_api.learner_resolver.default_learner_resolver`).

    Checks run in this order: sign-in (401), course key (400), ``learner_id`` is a valid id (404), resolver
    (404 or 403). A ``learner_id`` that is not valid, including an empty value, and any 404 from the resolver all
    give the same bare 404.

    The response is not paginated and carries nothing that identifies the learner.

    **Example Response**

    .. code-block:: json

        {
            "competency_statuses": [
                {"tag_id": 12, "status": "Demonstrated",
                 "demonstrated_in_course": {"course_key": "course-v1:Org1+Python200+Fall2026", "title": "Python 200"}},
                {"tag_id": 13, "status": "PartiallyAttempted", "demonstrated_in_course": null}
            ],
            "criterion_statuses": [
                {"criterion_id": 7, "status": "Demonstrated"}
            ]
        }
    """

    authentication_classes = (JwtAuthentication, SessionAuthenticationAllowInactiveUser)
    permission_classes = [IsAuthenticated]
    http_method_names = ["get"]

    def get(self, request, course_key):
        """Return the statuses of the learner the resolver chooses, after the key and ``learner_id`` are checked."""
        try:
            plain_course_key = parse_plain_course_key(course_key)
        except InvalidKeyError as exc:
            raise DRFValidationError(
                {"course_key": [_("'%(value)s' is not a valid course run key.") % {"value": course_key}]}
            ) from exc
        raw_learner_id = request.query_params.get("learner_id")
        if raw_learner_id is not None and not _LEARNER_ID.fullmatch(raw_learner_id):
            raise Http404
        learner_id = None if raw_learner_id is None else int(raw_learner_id)

        try:
            learner = resolve_learner(request, plain_course_key, learner_id)
        except (Http404, NotFound):
            # Re-raised bare so that a message from a host resolver cannot tell "not visible" from "no such user".
            raise Http404 from None

        result = get_learner_competency_statuses(user_id=learner.pk, course_key=plain_course_key)
        return Response(LearnerCompetencyStatusesSerializer(result).data)
