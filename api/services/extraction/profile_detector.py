import re

from api.models import ExtractionProfile


class ProfileDetector:
    def detect(self, document):
        haystack = str(document.get("text", "")).lower()
        best = None
        for profile in ExtractionProfile.objects.filter(active=True):
            required = [str(x).lower() for x in (profile.required_anchors or [])]
            optional = [str(x).lower() for x in (profile.optional_anchors or [])]
            matched_required = [x for x in required if x in haystack]
            if required and len(matched_required) != len(required):
                continue
            matched_optional = [x for x in optional if x in haystack]
            required_score = 1.0 if not required else len(matched_required) / len(required)
            optional_score = len(matched_optional) / len(optional) if optional else 1.0
            supplier_score = 0.0
            if profile.supplier_pattern:
                supplier_score = 1.0 if re.search(profile.supplier_pattern, haystack, re.I) else 0.0
            elif required:
                supplier_score = 1.0
            confidence = (required_score * 0.70) + (optional_score * 0.20) + (supplier_score * 0.10)
            candidate = {"profile": profile, "confidence": round(confidence, 4),
                         "matched_required": matched_required, "matched_optional": matched_optional}
            if best is None or candidate["confidence"] > best["confidence"]:
                best = candidate
        return best or {"profile": None, "confidence": 0.0, "matched_required": [], "matched_optional": []}
