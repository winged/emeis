"""Module to test api in a generic way."""

import hashlib
import json
import re

import inflection
import pytest
from django.db import connection
from django.db.models.fields.related import ManyToManyDescriptor
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework_json_api.renderers import JSONRenderer

from emeis.core.models import Scope

from ..views import (
    ACLViewSet,
    PermissionViewSet,
    RoleViewSet,
    ScopeViewSet,
    UserViewSet,
)

# @pytest.fixture(params=["admin_client", "authenticated_client"])
# def api_client(db, request):
#     client = request.getfixturevalue(request.param)
#     return client


@pytest.fixture()
def deterministic_uuids(mocker):
    # md5 hex digests are the same length as UUIDs, so django happily accepts
    # them.  Note also this has no cryptographic use here, so md5 is totally
    # fine
    hashy = hashlib.md5()

    def next_uuid():
        # add some string to the hash. This modifies it enough to yield a
        # totally different value
        hashy.update(b"x")
        # format: 9336ebf2-5087-d91c-818e-e6e9 ec29 f8c1
        # lengths: 8        4    4    4    12
        digest = hashy.hexdigest()
        return "-".join(
            [
                digest[:8],  # 8
                digest[8:12],  # 4
                digest[12:16],  # 4
                digest[16:20],  # 4
                digest[20:],  # 4
            ]
        )

    mocker.patch("uuid.uuid4", next_uuid)


@pytest.fixture(
    params=[
        # add your viewset and expected queries run for generic api testing...
        ACLViewSet,
        PermissionViewSet,
        RoleViewSet,
        ScopeViewSet,
        UserViewSet,
    ]
)
def viewset(request):
    """
    Viewset fixture listing viewsets for generic testing.

    For generic testing viewset needs to meet following requirements:
    * class fields `serializer_class` and `queryset`
    * registered factory for model it serves
    * registered uri with `SimpleRouter`
    """
    viewset_instance = request.param()
    model = viewset_instance.queryset.model
    instance_name = inflection.underscore(model._meta.object_name)
    base_name = model._meta.object_name.lower()

    viewset_instance.factory_name = f"{instance_name}_factory"
    viewset_instance.instance_name = instance_name
    viewset_instance.base_name = base_name
    viewset_instance.kwargs = {}

    return viewset_instance


@pytest.fixture
def fixture(
    deterministic_uuids,
    django_db_reset_sequences,
    request,
    viewset,
):
    """Get fixture and many to many relations of given viewset."""
    fixture = request.getfixturevalue(viewset.factory_name)()
    if hasattr(viewset.serializer_class, "included_serializers"):
        for name in sorted(viewset.serializer_class.included_serializers.keys()):
            relation_type = getattr(fixture.__class__, name)
            # pytest factory boy doesn't have native ManyToMany support
            # so needs to be handled manually
            if isinstance(relation_type, ManyToManyDescriptor):
                request.getfixturevalue("{0}_{1}".format(viewset.instance_name, name))

    return fixture


def prefetch_query_in_normalizer(query):
    """
    Normalize `IN` queries.

    Using `prefetch_related()` leads to `IN` queries where we have no control over the
    order of the parameters. Make sure the order in the snapshot is always
    alphabetical.
    """
    regex = r".* IN \((.*)\).*"
    match = re.match(regex, query)
    if match and match.groups():
        for group in match.groups():
            lst = group.split(", ")
            new_string = ", ".join(sorted(lst))
            query = query.replace(group, new_string)
    return query


def assert_response(response, query_context, snapshot, include_json=True):
    value = {
        "status": response.status_code,
        "queries": [
            prefetch_query_in_normalizer(query["sql"])
            for query in query_context.captured_queries
        ],
        "request": {
            k: v for k, v in response.request.items() if not k.startswith("wsgi")
        },
    }

    # Drop `SAVEPOINT` statements because they will change on every run.
    value["queries"] = list(
        filter(lambda lst: "SAVEPOINT" not in lst, value["queries"])
    )

    if include_json:
        value["response"] = response.json()

    snapshot.assert_match(value)


@pytest.mark.freeze_time("2017-05-21")
def test_api_list(fixture, request, admin_client, snapshot, viewset):
    url = reverse("{0}-list".format(viewset.base_name))

    # create data for proper num queries check
    request.getfixturevalue(viewset.factory_name).create_batch(2)

    included = getattr(viewset.serializer_class, "included_serializers", {})
    with CaptureQueriesContext(connection) as context:
        response = admin_client.get(url, data={"include": ",".join(included.keys())})

    assert_response(response, context, snapshot)


@pytest.mark.freeze_time("2017-05-21")
def test_api_detail(fixture, admin_client, viewset, snapshot):
    url = reverse("{0}-detail".format(viewset.base_name), args=[fixture.pk])

    included = getattr(viewset.serializer_class, "included_serializers", {})
    with CaptureQueriesContext(connection) as context:
        response = admin_client.get(url, data={"include": ",".join(included.keys())})

    assert_response(response, context, snapshot)


@pytest.mark.freeze_time("2017-05-21")
def test_api_create(
    transactional_db, deterministic_uuids, fixture, admin_client, viewset, snapshot
):
    url = reverse("{0}-list".format(viewset.base_name))

    serializer = viewset.serializer_class(fixture)
    renderer = JSONRenderer()
    context = {"view": viewset}
    data = renderer.render(serializer.data, renderer_context=context)
    fixture.delete()  # avoid constraint issues

    with CaptureQueriesContext(connection) as context:
        response = admin_client.post(url, data=json.loads(data))

    assert_response(response, context, snapshot)


@pytest.mark.freeze_time("2017-05-21")
def test_api_patch(fixture, admin_client, viewset, snapshot):
    if isinstance(fixture, Scope):
        # We need to get Scope through it's Tree manager, to get access to it's
        # CTE attributes
        fixture = Scope.objects.get(pk=fixture.pk)
    url = reverse("{0}-detail".format(viewset.base_name), args=[fixture.pk])

    serializer = viewset.serializer_class(fixture)
    renderer = JSONRenderer()
    context = {"view": viewset}
    data = renderer.render(serializer.data, renderer_context=context)

    with CaptureQueriesContext(connection) as context:
        response = admin_client.patch(url, data=json.loads(data))

    assert_response(response, context, snapshot)


@pytest.mark.freeze_time("2017-05-21")
def test_api_destroy(fixture, admin_client, snapshot, viewset):
    url = reverse("{0}-detail".format(viewset.base_name), args=[fixture.pk])

    with CaptureQueriesContext(connection) as context:
        response = admin_client.delete(url)

    assert_response(response, context, snapshot, False)


@pytest.mark.parametrize(
    "set_new_parent_to, expect_error",
    [
        ("self", "A node cannot be made a descendant of itself."),
        ("child", "A node cannot be made a descendant of itself."),
    ],
)
def test_validate_circular_parents(
    admin_client, scope_factory, set_new_parent_to, expect_error
):
    scope_1 = scope_factory()
    scope_2 = scope_factory(parent=scope_1)
    scope_3 = scope_factory(parent=scope_2)

    new_parent = {"self": scope_2.pk, "child": scope_3.pk}[set_new_parent_to]

    url = reverse("scope-detail", args=[scope_2.pk])
    data = {
        "data": {
            "attributes": {},
            "relationships": {
                "parent": {"data": {"type": "scopes", "id": str(new_parent)}}
            },
            "id": str(scope_2.pk),
            "type": "scopes",
        }
    }

    with pytest.raises(Exception) as excinfo:
        admin_client.patch(url, data)

    assert excinfo.match(expect_error)
