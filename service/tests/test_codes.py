from app.codes import ALPHABET, generate_code, normalize_code


def test_generate_shape():
    for _ in range(200):
        code = generate_code()
        assert len(code) == 12 and code.startswith("SL-") and code[7] == "-"
        assert all(ch in ALPHABET for ch in code.replace("-", "")[2:])


def test_normalize_variants():
    code = generate_code()
    compact = code.replace("-", "")
    assert normalize_code(code) == code
    assert normalize_code(compact.lower()) == code
    assert normalize_code(f" {code[:7]} {code[8:]} ") == code
    assert normalize_code(compact[2:]) == code  # body only


def test_normalize_rejects_garbage():
    assert normalize_code("") is None
    assert normalize_code("hello") is None
    assert normalize_code("SL-1234") is None
