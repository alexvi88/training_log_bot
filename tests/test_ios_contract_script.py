"""Разбор Swift в `scripts/ios_contract.py` на крошечном «приложении»: правила
обязательности, которые держат контрактный тест, проверены отдельно от настоящих моделей."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ios_contract  # noqa: E402

CLIENT = '''
final class APIClient {
    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    func thing(id: Int) async throws -> Thing {
        try await request("GET", "/things/\\(id)?full=1")
    }

    func maybe() async throws -> Thing? {
        try await requestOptional("GET", "/maybe")
    }

    func list() async throws -> [Thing] {
        var path = "/things"
        path += "?x=1"
        return try await request("GET", path)
    }

    func wrapped() async throws -> [Thing] {
        struct Response: Decodable { let items: [Thing]; let total: Int }
        let response: Response = try await request("POST", "/wrapped", body: Body())
        return response.items
    }

    func ignored() async throws {
        let _: EmptyResponse = try await request("DELETE", "/things/1")
    }
}
'''

MODELS = '''
struct Thing: Decodable {
    let id: Int
    let title: String
    let note: String?
    let fixed = 1                    // let со значением синтезатор не декодирует
    var withDefault: Int = 0         // var со значением ключ всё равно требует
    var optionalDefault: String? = nil
    let tags: [String]
    let inner: Inner
    var computed: Int { id + 1 }     // вычисляемое — не поле
    static let shared = 1

    struct Inner: Decodable {
        let bestE1rm: Double
        let kept: Bool
        let dropped: Int = 0
        enum CodingKeys: String, CodingKey {
            case bestE1rm = "bestE1Rm"
            case kept
        }
    }
}

struct Custom: Decodable {
    let a: String
    let b: Int
    let c: Bool
    let d: Int
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        a = try container.decode(String.self, forKey: .a)
        b = try container.decodeIfPresent(Int.self, forKey: .b) ?? 0
        c = (try? container.decode(Bool.self, forKey: .c)) ?? false
        d = try container.decode(Int.self, forKey: .d)
    }
    private enum CodingKeys: String, CodingKey { case a, b, c, d }
}
'''


def _app(tmp_path: Path, models: str = MODELS) -> Path:
    (tmp_path / "TrainingLog" / "Networking").mkdir(parents=True)
    (tmp_path / "TrainingLog" / "Models").mkdir()
    (tmp_path / "TrainingLog" / "Networking" / "APIClient.swift").write_text(CLIENT)
    (tmp_path / "TrainingLog" / "Models" / "Models.swift").write_text(models + "\nstruct Holder: Decodable { let c: Custom }\n")
    return tmp_path


def test_field_rules(tmp_path):
    manifest = ios_contract.build_manifest(_app(tmp_path))
    thing = manifest["types"]["Thing"]["fields"]
    assert {k: v["optional"] for k, v in thing.items()} == {
        "id": False, "title": False, "note": True, "withDefault": False, "optionalDefault": True,
        "tags": False, "inner": False,
    }
    assert thing["tags"]["type"] == "[String]" and thing["inner"]["type"] == "Thing.Inner"
    inner = manifest["types"]["Thing.Inner"]["fields"]
    assert set(inner) == {"bestE1Rm", "kept"}  # CodingKeys: ключ из raw value, `dropped` без ключа


def test_custom_init_marks_lenient_fields_optional(tmp_path):
    app = _app(tmp_path)
    (app / "TrainingLog/Networking/APIClient.swift").write_text(
        CLIENT.replace("func ignored", 'func custom() async throws -> Custom {\n try await request("GET", "/custom")\n }\n func ignored')
    )
    fields = ios_contract.build_manifest(app)["types"]["Custom"]["fields"]
    assert {k: v["optional"] for k, v in fields.items()} == {"a": False, "b": True, "c": True, "d": False}


def test_endpoints(tmp_path):
    manifest = ios_contract.build_manifest(_app(tmp_path))
    got = {(e["method"], e["path"]): (e["root"], e["nullable"]) for e in manifest["endpoints"]}
    assert got == {
        ("GET", "/things/{}"): ("Thing", False),
        ("GET", "/maybe"): ("Thing", True),
        ("GET", "/things"): ("[Thing]", False),
        ("POST", "/wrapped"): ("APIClient.wrapped().Response", False),
    }
    assert manifest["types"]["APIClient.wrapped().Response"]["fields"]["items"]["type"] == "[Thing]"


def test_check_mode(tmp_path, capsys):
    app = _app(tmp_path / "ios")
    out = tmp_path / "manifest.json"
    assert ios_contract.main(["--ios", str(app), "--out", str(out)]) == 0
    assert ios_contract.main(["--ios", str(app), "--out", str(out), "--check"]) == 0
    (app / "TrainingLog/Models/Models.swift").write_text(MODELS.replace("let note: String?", "let note: String") + "struct Holder: Decodable { let c: Custom }\n")
    assert ios_contract.main(["--ios", str(app), "--out", str(out), "--check"]) == 1
    assert "устарел" in capsys.readouterr().err
