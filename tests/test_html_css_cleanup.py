import unittest
from email.message import EmailMessage

from mail2yandex import clean_notification_body, format_message


CSS = (
    "body{margin:0;padding:8px;}\n"
    "p{line-height:1.15;margin:0;white-space:pre-wrap;}\n"
    "ol,ul{margin-top:0;margin-bottom:0;}\n"
    "img{border:none;}\n"
    "li>p{display:inline;}"
)


class MailCssCleanupTests(unittest.TestCase):
    def test_css_lines_surrounded_by_comment(self):
        source = (
            "Уважаемый(ая) IT-отдел! Тема: Test\n"
            "В задание ID#6982 добавлен комментарий:\n"
            + CSS + "\n"
            "Чистяков Кирилл Сергеевич 25.11.26 01:34 Test CSS"
        )
        actual = clean_notification_body(source)
        self.assertNotIn("margin:0", actual)
        self.assertNotIn("display:inline", actual)
        self.assertIn("В задание ID#6982 добавлен комментарий:", actual)
        self.assertIn("25.11.26 01:34\nTest CSS", actual)

    def test_task_notification_line_breaks(self):
        source = (
            "Уважаемый(ая) IT-отдел!Тема: Test"
            "В задание ID#6982 от kirill@example.org добавлен комментарий:\n"
            "Чистяков Кирилл Сергеевич 25.11.26 01:50Test #2"
        )
        cleaned = clean_notification_body(source)
        self.assertIn("IT-отдел!\nТема: Test\nВ задание", cleaned)
        self.assertIn("25.11.26 01:50\nTest #2", cleaned)

    def test_existing_line_breaks_not_doubled(self):
        source = (
            "Уважаемый(ая) IT-отдел!\nТема: Test\nВ задание ID#6982\n"
            "Чистяков Кирилл Сергеевич 25.11.26 01:50\nTest #2"
        )
        self.assertEqual(clean_notification_body(source), source)

    def test_comment_label_collapses_many_blank_lines(self):
        source = (
            "В задание ID#6982 от user@example.org добавлен комментарий:"
            "\n\n \n\u00a0\n\n\n\t\n\n"
            "Чистяков Кирилл Сергеевич 25.11.26 02:05\nпроверь визуал"
        )
        actual = clean_notification_body(source)
        self.assertIn(
            "добавлен комментарий:\nЧистяков Кирилл Сергеевич 25.11.26 02:05\nпроверь визуал",
            actual,
        )

    def test_comment_label_without_blank_lines(self):
        source = "В задание ID#6982 добавлен комментарий:Автор комментария"
        self.assertEqual(
            clean_notification_body(source),
            "В задание ID#6982 добавлен комментарий:\nАвтор комментария",
        )

    def test_markdown_bold_task_id_timestamp_and_comment(self):
        message = EmailMessage()
        message["Subject"] = "Добавлен комментарий"
        message.set_content(
            "Уважаемый(ая) IT-отдел!\n"
            "В задание ID#6982 от user@example.org добавлен комментарий:\n"
            "Чистяков Кирилл Сергеевич 25.11.26 02:05\n"
            "проверь визуал"
        )
        result = format_message(message.as_bytes(), "from@example.org", ["to@example.org"], 5500)
        self.assertIn("**В задание ID#6982** от user@example.org", result)
        self.assertIn("Чистяков Кирилл Сергеевич **25.11.26 02:05**", result)
        self.assertIn("\n**проверь визуал**", result)

    def test_no_bold_for_regular_mail(self):
        message = EmailMessage()
        message["Subject"] = "Сводка"
        message.set_content("Проверка статуса 25.11.26 02:05\nвсё работает")
        result = format_message(message.as_bytes(), "from@example.org", ["to@example.org"], 5500)
        self.assertNotIn("**", result)

    def test_multiline_comment_highlight(self):
        message = EmailMessage()
        message.set_content(
            "В задание ID#42 добавлен комментарий:\n"
            "Автор 25.11.26 02:05\n"
            "Первая строка\nВторая строка"
        )
        result = format_message(message.as_bytes(), "from@example.org", ["to@example.org"], 5500)
        self.assertIn("**Первая строка**\n**Вторая строка**", result)

    def test_css_inline_with_prose(self):
        actual = clean_notification_body("Описание body{margin:0;padding:8px;} продолжается")
        self.assertEqual(actual, "Описание  продолжается")

    def test_mail_format_keeps_visible_content(self):
        message = EmailMessage()
        message["Subject"] = "Test"
        message.set_content("Комментарий:\n" + CSS + "\nРезультат: готово")
        result = format_message(message.as_bytes(), "sender@example.org", ["staff@example.org"], 5500)
        self.assertIn("Тема: Test", result)
        self.assertIn("Результат: готово", result)
        self.assertNotIn("white-space:pre-wrap", result)

    def test_non_css_braces_preserved(self):
        self.assertEqual(clean_notification_body("Текст hello{note: keep this}"), "Текст hello{note: keep this}")


if __name__ == "__main__":
    unittest.main()
