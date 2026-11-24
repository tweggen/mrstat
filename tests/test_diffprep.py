from mrstat.diffprep import CleanOptions, category, clean, facts_line, is_comment, is_import

BLOCK = ["def compute_total(items):", "    subtotal = sum(i.price for i in items)",
         "    tax_amount = subtotal * TAX_RATE", "    return subtotal + tax_amount"]


def d(*hunks):
    return "\n".join(hunks) + "\n"


def test_whitespace_only_changes_disappear():
    diff = d("@@ -1,3 +1,3 @@ def f():", " a = 1", "-    x = compute( 1,2 )", "+    x = compute(1, 2)",
             "+", " b = 2")
    f = clean([("m.py", "modified", diff)], CleanOptions())[0]
    assert f.hunks == [] and f.facts.whitespace == 3 and (f.facts.added, f.facts.deleted) == (0, 0)


def test_moved_block_across_files_becomes_marker():
    old = d("@@ -10,6 +10,2 @@ class A:", " keep_me = True", *[f"-{l}" for l in BLOCK], " other = 1")
    new = d("@@ -1,0 +1,5 @@", *[f"+    {l}" for l in BLOCK], "+NEW_CONSTANT = 3")  # re-indented
    a, b = clean([("a.py", "modified", old), ("b.py", "added", new)], CleanOptions())
    assert "~ [4 lines moved to b.py]" in a.render() and "subtotal" not in a.render()
    assert "~ [4 lines moved from a.py]" in b.render() and "+NEW_CONSTANT = 3" in b.render()
    assert a.facts.moved == 4 and b.facts.moved == 4 and b.facts.added == 1


def test_short_common_lines_are_not_moves():
    old = d("@@ -1,3 +1,0 @@", "-}", "-return x;", "-}")
    new = d("@@ -1,0 +1,3 @@", "+}", "+return x;", "+}")
    a, b = clean([("a.java", "modified", old), ("b.java", "modified", new)], CleanOptions())
    assert a.facts.moved == 0 and a.facts.deleted == 3


def test_comments_imports_and_context():
    diff = d("@@ -1,9 +1,10 @@ void run()", " ctx1", " ctx2", " ctx3", "+import java.util.List;",
             "+// explain the loop", "+for (int i = 0; i < n; i++) total += i;", "+*ptr = 5;",
             " ctx4", " ctx5", " ctx6")
    f = clean([("src/Run.java", "modified", diff)], CleanOptions(context_lines=1))[0]
    text = f.render()
    assert "import" not in text and "explain" not in text and "*ptr = 5;" in text
    assert " ctx3" in text and " ctx4" in text and " ctx1" not in text and " ctx6" not in text
    assert (f.facts.imports, f.facts.comments, f.facts.added) == (1, 1, 2)
    assert text.startswith("@@ void run()")
    assert "in: void run()" in facts_line(f.facts)


def test_categories_and_language_rules():
    assert category("src/main/java/A.java") == "code"
    assert category("src/test/java/ATest.java") == "test"
    assert category("tests/test_x.py") == "test"
    assert category("src/FooTests.cs") == "test" and category("lib/foo.spec.ts") == "test"
    assert category("mrstat/selftest.py") == "code" and category("src/contest.py") == "code"
    assert category("docs/guide.md") == "docs" and category(".gitlab-ci.yml") == "config"
    assert is_comment("# note", "python") and not is_comment("# not a comment", "c")
    assert is_import("#include <vector>", "cpp") and is_import("from a import b", "python")
    assert not is_import("important = 1", "python")
