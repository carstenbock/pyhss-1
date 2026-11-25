# Subscriber self-care view of the MMTel supplementary services.
# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read and change the supplementary services subscribers may set themselves.

The services live in ``ims_subscriber.xcap_profile``: an Sh-Data envelope with
MMTEL-Services repository data (TS 29.328 clause 7.6.1) holding simservs
elements (TS 24.623, ETSI TS 183 023). The MMTel-AS reads them through the
sh-daemon (``simplify_user_data``), so the element paths used here are the
ones the sh-daemon evaluates.

Only these settings are exposed (ADR-0052):

- call forwarding CFU, CFB, CFNA (with the no-reply timer), CFNR (TS 24.604)
- OIR (TS 24.607), ACR (TS 24.611), communication waiting (TS 24.615)
- the announcement language (platform extension ``<ss:language>``)

``apply_changes`` edits the stored XML in place and touches only these
elements. Barring, operator settings, other forwarding rules (for example
CFNRc) and unknown elements stay as they are.
"""

import re
import xml.etree.ElementTree as ET

NS_SS = "http://uri.etsi.org/ngn/params/xml/simservs/xcap"
NS_CP = "urn:ietf:params:xml:ns:common-policy"
SERVICE_INDICATION = "MMTEL-Services"

ET.register_namespace("ss", NS_SS)
ET.register_namespace("cp", NS_CP)

# Forwarding variant -> the cp:conditions child that selects it (None = CFU).
CF_VARIANTS = {
    "cfu": None,
    "cfb": "busy",
    "cfna": "no-answer",
    "cfnr": "not-registered",
}

# On/off services: setting -> (container, child carrying the "active" attribute).
# A child of None means the attribute sits on the container itself.
TOGGLES = {
    "cw": ("complete-communication-waiting", "communication-waiting"),
    "oir": ("complete-originating-identity-restriction",
            "originating-identity-presentation-restriction"),
    "acr": ("anonymous-communication-rejection", None),
}

# What the MMTel-AS applies when an element is absent (sh-daemon
# simplify_user_data: flags default to false, the timer to 30 s).
DEFAULT_NO_REPLY_TIMER = 30

# TS 24.604 XML schema: the NoReplyTimer ranges from 5 to 180 seconds.
NO_REPLY_TIMER_MIN = 5
NO_REPLY_TIMER_MAX = 180

_E164 = re.compile(r"^\+[1-9][0-9]{5,14}$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


class SelfcareError(ValueError):
    """A change request the subscriber may not make, or a malformed one."""


def _q(ns, name):
    return f"{{{ns}}}{name}"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _truthy(value):
    return str(value or "").strip().lower() == "true"


# ---------------------------------------------------------------------------
# Locating the MMTelServices container
# ---------------------------------------------------------------------------

def _new_envelope():
    root = ET.Element("Sh-Data")
    repo = ET.SubElement(root, "RepositoryData")
    ET.SubElement(repo, "ServiceIndication").text = SERVICE_INDICATION
    ET.SubElement(repo, "SequenceNumber").text = "0"
    service_data = ET.SubElement(repo, "ServiceData")
    ET.SubElement(service_data, _q(NS_SS, "MMTelServices"))
    return root


def _mmtel_repository(root):
    for repo in root.findall("RepositoryData"):
        si = repo.find("ServiceIndication")
        if si is not None and (si.text or "").strip() == SERVICE_INDICATION:
            return repo
    return None


def _load(profile_xml):
    """Return (root, repository, MMTelServices element), creating what is missing.

    The HSS serves only RepositoryData of an Sh-Data envelope over Sh, so a
    bare simservs or MMTelServices document is moved into one.
    """
    if not (profile_xml or "").strip():
        root = _new_envelope()
    else:
        root = ET.fromstring(profile_xml)
        if root.tag != "Sh-Data":
            bare = root
            root = _new_envelope()
            container = root.find(f"RepositoryData/ServiceData/{_q(NS_SS, 'MMTelServices')}")
            for child in list(bare):
                container.append(child)
    repo = _mmtel_repository(root)
    if repo is None:
        fresh = _new_envelope().find("RepositoryData")
        root.append(fresh)
        repo = fresh
    service_data = repo.find("ServiceData")
    if service_data is None:
        service_data = ET.SubElement(repo, "ServiceData")
    container = None
    for elem in service_data.iter():
        if elem.tag in (_q(NS_SS, "MMTelServices"), _q(NS_SS, "simservs")):
            container = elem
            break
    if container is None:
        container = ET.SubElement(service_data, _q(NS_SS, "MMTelServices"))
    return root, repo, container


def _diversion(container, create):
    cdiv = container.find(_q(NS_SS, "complete-communication-diversion"))
    if cdiv is None:
        if not create:
            return None
        cdiv = ET.SubElement(container, _q(NS_SS, "complete-communication-diversion"))
    cd = cdiv.find(_q(NS_SS, "communication-diversion"))
    if cd is None:
        if not create:
            return None
        cd = ET.SubElement(cdiv, _q(NS_SS, "communication-diversion"))
    return cd


def _variant_of(rule):
    """Forwarding variant of a cp:rule, classified like the sh-daemon does.

    Returns None for rules that do not forward or that this module does not
    expose (for example CFNRc, "not-reachable").
    """
    actions = rule.find(_q(NS_CP, "actions"))
    if actions is None or not any(_local(a.tag) == "forward-to" for a in actions):
        return None
    conditions = rule.find(_q(NS_CP, "conditions"))
    names = {_local(c.tag) for c in conditions} if conditions is not None else set()
    if not names:
        return "cfu"
    for name in ("not-registered", "not-reachable", "busy", "no-answer"):
        if name in names:
            return {"not-registered": "cfnr", "busy": "cfb", "no-answer": "cfna"}.get(name)
    return None


def _forward_to(rule):
    actions = rule.find(_q(NS_CP, "actions"))
    for action in actions:
        if _local(action.tag) == "forward-to":
            return action
    return None


def _child_text(elem, name):
    for child in elem:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _rules(cd):
    if cd is None:
        return []
    return [(ruleset, rule)
            for ruleset in cd.findall(_q(NS_CP, "ruleset"))
            for rule in ruleset.findall(_q(NS_CP, "rule"))]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_settings(profile_xml):
    """The exposed settings of a stored profile, as the MMTel-AS applies them."""
    _, _, container = _load(profile_xml)
    settings = {
        "call_forwarding": {v: {"active": False, "target": ""} for v in CF_VARIANTS},
        "no_reply_timer": DEFAULT_NO_REPLY_TIMER,
        "language": "",
    }
    for key, (name, child) in TOGGLES.items():
        elem = container.find(_q(NS_SS, name))
        if elem is not None and child is not None:
            elem = elem.find(_q(NS_SS, child))
        settings[key] = elem is not None and _truthy(elem.get("active"))

    language = container.find(_q(NS_SS, "language"))
    if language is not None and (language.text or "").strip():
        settings["language"] = language.text.strip()

    cd = _diversion(container, create=False)
    if cd is not None:
        timer = cd.find(_q(NS_SS, "NoReplyTimer"))
        if timer is not None:
            try:
                settings["no_reply_timer"] = int((timer.text or "").strip())
            except ValueError:
                pass
    for _, rule in _rules(cd):
        variant = _variant_of(rule)
        if variant is None:
            continue
        target = _child_text(_forward_to(rule), "target")
        if target:
            settings["call_forwarding"][variant] = {"active": True, "target": target}
    return settings


# ---------------------------------------------------------------------------
# Validating a change request
# ---------------------------------------------------------------------------

def normalise_target(raw):
    """A forwarding target as ``tel:+<E.164>``; accepts ``+49...`` or ``tel:+49...``."""
    value = re.sub(r"[\s\-./()]", "", str(raw or ""))
    if value.lower().startswith("tel:"):
        value = value[4:]
    if not _E164.match(value):
        raise SelfcareError(f"forwarding target {raw!r} is not an international number (+E.164)")
    return "tel:" + value


def validate_changes(body, allowed_prefixes):
    """Check a change request and return it normalised.

    Only the exposed settings are accepted. A forwarding target must start
    with one of ``allowed_prefixes`` (E.164 with "+"); with no prefixes
    configured, forwarding cannot be switched on.
    """
    if not isinstance(body, dict):
        raise SelfcareError("request body must be a JSON object")
    allowed_keys = {"call_forwarding", "no_reply_timer", "language", *TOGGLES}
    unknown = set(body) - allowed_keys
    if unknown:
        raise SelfcareError(f"settings not changeable here: {', '.join(sorted(unknown))}")

    changes = {}
    for key in TOGGLES:
        if key in body:
            if not isinstance(body[key], bool):
                raise SelfcareError(f"{key} must be true or false")
            changes[key] = body[key]

    if "no_reply_timer" in body:
        timer = body["no_reply_timer"]
        if isinstance(timer, bool) or not isinstance(timer, int) \
                or not NO_REPLY_TIMER_MIN <= timer <= NO_REPLY_TIMER_MAX:
            raise SelfcareError(
                f"no_reply_timer must be {NO_REPLY_TIMER_MIN} to {NO_REPLY_TIMER_MAX} seconds")
        changes["no_reply_timer"] = timer

    if "language" in body:
        language = body["language"]
        if not isinstance(language, str) or (language and not _LANGUAGE.match(language)):
            raise SelfcareError("language must be a language tag such as de-DE, or empty")
        changes["language"] = language

    if "call_forwarding" in body:
        forwarding = body["call_forwarding"]
        if not isinstance(forwarding, dict):
            raise SelfcareError("call_forwarding must be an object")
        unknown = set(forwarding) - set(CF_VARIANTS)
        if unknown:
            raise SelfcareError(f"forwarding variants not changeable here: {', '.join(sorted(unknown))}")
        changes["call_forwarding"] = {}
        for variant, entry in forwarding.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("active"), bool):
                raise SelfcareError(f"call_forwarding.{variant} needs active: true or false")
            if not entry["active"]:
                changes["call_forwarding"][variant] = {"active": False}
                continue
            target = normalise_target(entry.get("target"))
            number = target[4:]
            if not any(number.startswith(p) for p in allowed_prefixes):
                raise SelfcareError(f"forwarding to {number} is not allowed")
            changes["call_forwarding"][variant] = {"active": True, "target": target}
    return changes


# ---------------------------------------------------------------------------
# Applying a change request
# ---------------------------------------------------------------------------

def _set_toggle(container, name, child, value):
    elem = container.find(_q(NS_SS, name))
    if elem is None:
        elem = ET.SubElement(container, _q(NS_SS, name))
    if child is not None:
        leaf = elem.find(_q(NS_SS, child))
        if leaf is None:
            leaf = ET.SubElement(elem, _q(NS_SS, child))
        elem = leaf
    elem.set("active", "true" if value else "false")


def _new_rule(variant, target, notify):
    # SPEC-DEVIATION: TS 24.604 XML schema -- the conditions and forward-to
    # belong to the simservs namespace. They are written in the common-policy
    # namespace like the HSS-GUI and the sh-daemon do; all readers on the
    # platform match local names only.
    rule = ET.Element(_q(NS_CP, "rule"), {"id": "cdiv-" + variant})
    conditions = ET.SubElement(rule, _q(NS_CP, "conditions"))
    if CF_VARIANTS[variant]:
        ET.SubElement(conditions, _q(NS_CP, CF_VARIANTS[variant]))
    actions = ET.SubElement(rule, _q(NS_CP, "actions"))
    forward = ET.SubElement(actions, _q(NS_CP, "forward-to"))
    ET.SubElement(forward, _q(NS_CP, "target")).text = target
    ET.SubElement(forward, _q(NS_CP, "notify-caller")).text = notify
    if not CF_VARIANTS[variant]:
        # An empty cp:conditions element would still be unconditional; omit it.
        rule.remove(conditions)
    return rule


def _apply_forwarding(container, forwarding):
    cd = _diversion(container, create=True)
    existing = _rules(cd)
    for variant, entry in forwarding.items():
        notify = "false"
        for ruleset, rule in existing:
            if _variant_of(rule) == variant:
                notify = _child_text(_forward_to(rule), "notify-caller") or notify
                ruleset.remove(rule)
        if entry["active"]:
            ruleset = cd.find(_q(NS_CP, "ruleset"))
            if ruleset is None:
                ruleset = ET.SubElement(cd, _q(NS_CP, "ruleset"))
            ruleset.append(_new_rule(variant, entry["target"], notify))
    # The service is active while at least one forwarding rule exists.
    any_rule = any(_forward_to_present(rule) for _, rule in _rules(cd))
    cd.set("active", "true" if any_rule else "false")


def _forward_to_present(rule):
    actions = rule.find(_q(NS_CP, "actions"))
    return actions is not None and any(_local(a.tag) == "forward-to" for a in actions)


def _bump_sequence_number(repo):
    seq = repo.find("SequenceNumber")
    if seq is None:
        seq = ET.Element("SequenceNumber")
        repo.insert(1, seq)
    try:
        current = int((seq.text or "0").strip())
    except ValueError:
        current = 0
    # TS 29.328 clause 7.6.1: 0 to 65535; 0 is used only for the first
    # version, so the value after 65535 is 1.
    seq.text = str(current + 1 if current < 65535 else 1)


def apply_changes(profile_xml, changes):
    """Apply validated changes to a stored profile and return the new XML."""
    root, repo, container = _load(profile_xml)

    for key, (name, child) in TOGGLES.items():
        if key in changes:
            _set_toggle(container, name, child, changes[key])

    if "language" in changes:
        language = container.find(_q(NS_SS, "language"))
        if changes["language"]:
            if language is None:
                language = ET.SubElement(container, _q(NS_SS, "language"))
            language.text = changes["language"]
        elif language is not None:
            container.remove(language)

    if "no_reply_timer" in changes:
        cd = _diversion(container, create=True)
        timer = cd.find(_q(NS_SS, "NoReplyTimer"))
        if timer is None:
            timer = ET.Element(_q(NS_SS, "NoReplyTimer"))
            cd.insert(0, timer)
        timer.text = str(changes["no_reply_timer"])

    if "call_forwarding" in changes:
        _apply_forwarding(container, changes["call_forwarding"])

    _bump_sequence_number(repo)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
