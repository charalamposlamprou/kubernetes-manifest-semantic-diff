"""Offline resource identity, scope resolution, and path-specific list semantics."""

import base64
import json
from dataclasses import dataclass

from .input import Problem, documents, label

CLUSTER = {
    "": "Namespace Node PersistentVolume ComponentStatus",
    "admissionregistration.k8s.io": "ValidatingWebhookConfiguration MutatingWebhookConfiguration ValidatingAdmissionPolicy ValidatingAdmissionPolicyBinding MutatingAdmissionPolicy MutatingAdmissionPolicyBinding",
    "apiextensions.k8s.io": "CustomResourceDefinition",
    "apiregistration.k8s.io": "APIService",
    "authentication.k8s.io": "TokenReview SelfSubjectReview",
    "authorization.k8s.io": "SubjectAccessReview SelfSubjectAccessReview SelfSubjectRulesReview",
    "certificates.k8s.io": "CertificateSigningRequest ClusterTrustBundle",
    "flowcontrol.apiserver.k8s.io": "FlowSchema PriorityLevelConfiguration",
    "networking.k8s.io": "IngressClass IPAddress ServiceCIDR",
    "node.k8s.io": "RuntimeClass",
    "internal.apiserver.k8s.io": "StorageVersion",
    "policy": "PodSecurityPolicy",
    "rbac.authorization.k8s.io": "ClusterRole ClusterRoleBinding",
    "resource.k8s.io": "DeviceClass ResourceSlice DeviceTaintRule ResourcePoolStatusRequest",
    "scheduling.k8s.io": "PriorityClass",
    "storage.k8s.io": "StorageClass CSIDriver CSINode VolumeAttachment VolumeAttributesClass",
    "storagemigration.k8s.io": "StorageVersionMigration",
}
MISSING = object()


@dataclass(repr=False)
class Mask:
    value: object


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def flatten(raw):
    items = []

    def append(doc):
        if not isinstance(doc, dict):
            raise Problem("Manifest document must be a Kubernetes object.")
        if doc.get("kind") in ("List", "SecretList") and doc.get("apiVersion") == "v1":
            if not isinstance(doc.get("items"), list):
                raise Problem("Expected items in Kubernetes List.")
            for child in doc["items"]:
                if doc["kind"] == "SecretList":
                    if (
                        not isinstance(child, dict)
                        or child.get("kind", "Secret") != "Secret"
                        or child.get("apiVersion", "v1") != "v1"
                    ):
                        raise Problem("SecretList contains an invalid Secret object.")
                    child = {**child, "kind": "Secret", "apiVersion": "v1"}
                append(child)
        else:
            items.append(doc)

    for doc in documents(raw):
        append(doc)
    return items


def crd_scopes(docs):
    result = {}
    for doc in docs:
        if doc.get("kind") != "CustomResourceDefinition" or doc.get("apiVersion") not in (
            "apiextensions.k8s.io/v1",
            "apiextensions.k8s.io/v1beta1",
        ):
            continue
        try:
            spec = doc["spec"]
            key = label(spec["group"]), label(spec["names"]["kind"])
            if spec["scope"] not in ("Namespaced", "Cluster"):
                raise KeyError
            result[key] = spec["scope"] == "Cluster"
        except (KeyError, TypeError):
            raise Problem("CRD requires a valid group, kind and scope.") from None
    return result


def remember(value, known, decode=False):
    if isinstance(value, dict):
        for item in value.values():
            remember(item, known, decode)
    elif isinstance(value, list):
        for item in value:
            remember(item, known, decode)
    elif type(value) is str and value:
        known.add(value)
        if decode:
            try:
                decoded = base64.b64decode(value, validate=True).decode()
                if decoded:
                    known.add(decoded)
            except (ValueError, UnicodeError):
                pass


def protect(value, path, patterns, secret_root, known):
    if secret_root is None and isinstance(value, dict) and value.get("kind") == "Secret":
        secret_root = len(path)
        if any(k in value and not isinstance(value[k], dict) for k in ("data", "stringData")):
            raise Problem("Secret data and stringData must be mappings.")
    relative = path[secret_root:] if secret_root is not None else ()
    secret_field = (len(relative) == 2 and relative[0] in ("data", "stringData")) or relative == (
        "metadata",
        "annotations",
        "kubectl.kubernetes.io/last-applied-configuration",
    )
    if secret_field or any(
        len(pattern) == len(path)
        and all(a == "*" or a == b for a, b in zip(pattern, path, strict=True))
        for pattern in patterns
    ):
        remember(value, known, decode=relative[:1] == ("data",))
        return Mask(value)
    if isinstance(value, dict):
        return {k: protect(v, (*path, k), patterns, secret_root, known) for k, v in value.items()}
    if isinstance(value, list):
        return [
            protect(v, (*path, str(i)), patterns, secret_root, known) for i, v in enumerate(value)
        ]
    return value


def index(docs, namespace, patterns, known, cluster_scoped=()):
    scopes = dict.fromkeys(cluster_scoped, True)
    scopes.update(crd_scopes(docs))
    result = {}
    for obj in docs:
        api = obj.get("apiVersion")
        if not isinstance(api, str) or api.count("/") > 1:
            raise Problem("Invalid or missing resource apiVersion.")
        group, _, version = api.rpartition("/")
        label(version)
        if group:
            label(group)
        kind = label(obj.get("kind"))
        meta = obj.get("metadata")
        if not isinstance(meta, dict):
            raise Problem("Resource metadata and a stable name are required.")
        name = label_name(meta.get("name"))
        cluster = scopes.get((group, kind), kind in CLUSTER.get(group, "").split())
        ns = meta.get("namespace")
        if ns is not None and ns != "":
            label(ns)
            if cluster:
                raise Problem("Cluster-scoped resource specifies a namespace.")
        if cluster:
            meta.pop("namespace", None)
        else:
            meta["namespace"] = ns or namespace
        key = (group, kind, "" if cluster else meta["namespace"], name)
        if key in result:
            raise Problem("Duplicate group, kind, namespace and name within one target.")
        result[key] = protect(obj, (), patterns, None, known)
    return result


def label_name(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        raise Problem("Resource needs a stable metadata.name of at most 253 characters.")
    return value


def display(value):
    if value is MISSING:
        return "∅"
    if isinstance(value, Mask):
        return "[redacted]"
    return encoded(value)


def pod_container_path(group, kind):
    if (group, kind) == ("", "Pod"):
        return ("spec",)
    if (group, kind) == ("batch", "CronJob"):
        return ("spec", "jobTemplate", "spec", "template", "spec")
    if (group == "apps" and kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet")) or (
        group,
        kind,
    ) in {("batch", "Job"), ("", "ReplicationController")}:
        return ("spec", "template", "spec")
    return None


def fields(left, right, group, kind):
    pod = pod_container_path(group, kind)

    def walk(a, b, path, title):
        if isinstance(a, Mask) or isinstance(b, Mask):
            if not (
                isinstance(a, Mask) and isinstance(b, Mask) and encoded(a.value) == encoded(b.value)
            ):
                yield (
                    title,
                    "∅" if a is MISSING else "[redacted]",
                    "∅" if b is MISSING else "[redacted]",
                )
            return
        if isinstance(a, dict) or isinstance(b, dict):
            if isinstance(a, dict) and isinstance(b, dict) or a is MISSING or b is MISSING:
                aa, bb = ({} if a is MISSING else a), ({} if b is MISSING else b)
                if aa or bb:
                    for k in sorted(aa.keys() | bb.keys()):
                        yield from walk(
                            aa.get(k, MISSING),
                            bb.get(k, MISSING),
                            (*path, k),
                            f"{title}.{k}" if title else k,
                        )
                    return
        if isinstance(a, list) or isinstance(b, list):
            if isinstance(a, list) and isinstance(b, list) or a is MISSING or b is MISSING:
                aa, bb = ([] if a is MISSING else a), ([] if b is MISSING else b)
                if aa or bb:
                    keyed = pod and path in {
                        pod + ("containers",),
                        pod + ("volumes",),
                        pod + ("initContainers",),
                        pod + ("containers", "*", "env"),
                        pod + ("initContainers", "*", "env"),
                    }
                    keyed = keyed or (
                        (group, kind) == ("", "Service") and path == ("spec", "ports")
                    )
                    if keyed and all(
                        isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"]
                        for v in aa + bb
                    ):
                        ma, mb = ({v["name"]: v for v in seq} for seq in (aa, bb))
                        if len(ma) == len(aa) and len(mb) == len(bb):
                            if path[-1] in ("env", "initContainers") and [
                                k for k in ma if k in mb
                            ] != [k for k in mb if k in ma]:
                                yield (title + ".order", encoded(list(ma)), encoded(list(mb)))
                            for k in sorted(ma.keys() | mb.keys()):
                                yield from walk(
                                    ma.get(k, MISSING),
                                    mb.get(k, MISSING),
                                    (*path, "*"),
                                    f"{title}[{k}]",
                                )
                            return
                    for i in range(max(len(aa), len(bb))):
                        yield from walk(
                            aa[i] if i < len(aa) else MISSING,
                            bb[i] if i < len(bb) else MISSING,
                            (*path, "*"),
                            f"{title}[{i}]",
                        )
                    return

        # Shape changes can contain protected descendants. Summarize a collection's type.
        def simple(v):
            return (
                "{…}"
                if isinstance(v, dict) and v
                else "[…]"
                if isinstance(v, list) and v
                else display(v)
            )

        if type(a) is not type(b) or a != b:
            yield (title, simple(a), simple(b))

    yield from walk(left, right, (), "")
