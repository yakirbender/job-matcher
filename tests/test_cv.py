from pathlib import Path

import pytest

from job_matcher.cv import CvError, find_skills, load_cv, load_vocabulary, parse_cv, read_cv_text

VOCAB = load_vocabulary(Path(__file__).parents[1] / "config" / "skills.yaml")

# Same layout as a real CV, with invented content.
CV_TEXT = """Sam Example
sam@example.test | Montreal, QC
Languages & Technical Skills
●
Programming Languages: Java • Python • C • JavaScript • SQL
●
Frameworks: Spring Boot • React • Vue.js • Mockito
●
Testing: JUnit • Cucumber • Postman
Education
B.Sc. Computer Science Sept. 2019 - Feb. 2026
Software Projects
Inventory API (Java, Spring Boot, PostgreSQL)
- Built REST endpoints with unit tests
Other Work Experience
Peer Tutor - Object Oriented Programming
- Helped students with data structures
"""


def test_sections_and_skills_with_where_they_appear():
    cv = parse_cv(CV_TEXT, VOCAB)
    assert set(cv.sections) >= {"header", "skills", "education", "projects", "experience"}
    assert cv.skills["java"] == ["skills", "projects"]
    assert cv.skills["oop"] == ["experience"]
    assert cv.skills["postgresql"] == ["projects"]
    assert {"spring", "vue", "unit testing", "bdd", "api testing", "c"} <= set(cv.skills)
    assert "c++" not in cv.skills


def test_bare_ambiguous_names_count_in_the_cv_but_not_in_postings():
    posting = "Report to C-level executives. R&D budget. Go to market."
    assert find_skills(posting, VOCAB.jobs) == set()
    assert {"c", "go"} <= find_skills("Skills: C • Go • R", VOCAB.cv)
    assert {"c", "c++"} <= find_skills("Experience with C/C++", VOCAB.jobs)
    assert find_skills("Golang microservices", VOCAB.jobs) == {"go"}


def test_skill_matching_respects_word_boundaries():
    assert find_skills("JavaScript and TypeScript", VOCAB.jobs) == {"javascript", "typescript"}
    assert find_skills("C# and .NET Core", VOCAB.jobs) == {"c#", ".net"}
    assert find_skills("Programmation orientée objet, tests unitaires", VOCAB.jobs) == {
        "oop", "unit testing"
    }


def test_multi_word_tool_names_match_as_a_phrase():
    assert find_skills("Built with Claude Code.", VOCAB.jobs) == {"claude code"}
    assert find_skills("Claude from accounting wrote code.", VOCAB.jobs) == set()


def test_related_skills_must_be_defined(tmp_path):
    path = tmp_path / "skills.yaml"
    path.write_text("java: []\nrelated_skills:\n  kotlin: [java]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="kotlin"):
        load_vocabulary(path)
    assert VOCAB.related["angular"] == ["react", "vue"]


def test_bullets_on_their_own_line_are_joined(tmp_path):
    path = tmp_path / "cv.txt"
    path.write_text("Skills\n●\nJava\n•\nPython\n", encoding="utf-8")
    assert read_cv_text(path) == "Skills\n- Java\n- Python"


def test_markdown_and_docx_are_read(tmp_path):
    md = tmp_path / "cv.md"
    md.write_text(CV_TEXT, encoding="utf-8")
    assert "java" in load_cv(md, VOCAB).skills

    import docx

    document = docx.Document()
    for line in CV_TEXT.splitlines():
        document.add_paragraph(line)
    path = tmp_path / "cv.docx"
    document.save(str(path))
    assert "spring" in load_cv(path, VOCAB).skills


def test_unsupported_and_empty_files_fail_clearly(tmp_path):
    with pytest.raises(CvError, match="unsupported"):
        read_cv_text(tmp_path / "cv.rtf")
    empty = tmp_path / "cv.txt"
    empty.write_text("  \n", encoding="utf-8")
    with pytest.raises(CvError, match="scanned PDF"):
        read_cv_text(empty)
