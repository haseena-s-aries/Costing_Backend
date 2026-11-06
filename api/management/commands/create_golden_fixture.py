import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from api.management.commands.run_extraction_regression import FileShim
from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
from api.services.regression_testing import json_default

class Command(BaseCommand):
    help = "Create a clearly marked draft extraction golden fixture."
    def add_arguments(self, parser): parser.add_argument("filename"); parser.add_argument("--fixtures", default="test_fixtures")
    def handle(self, *args, **opts):
        path=Path(opts["fixtures"])/"inputs"/opts["filename"]
        if not path.exists(): raise CommandError(f"Missing input: {path}")
        out=Path(opts["fixtures"])/"expected"/"extraction"; out.mkdir(parents=True, exist_ok=True)
        result=ExtractionOrchestrator().extract(FileShim(path.read_bytes()), path.name, None)
        result["_draft_unverified"] = True
        draft=out/f"{path.stem}.draft.json"; draft.write_text(json.dumps(result, indent=2, default=json_default), encoding="utf-8")
        self.stdout.write(f"Created unverified draft: {draft}")
