from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from seleniumbase import BaseCase

from django.test import TestCase


class FHISeleniumBase(BaseCase, TestCase):
    def assert_title_eventually(self, desired_title):
        try:
            WebDriverWait(self.driver, 15).until(EC.title_is(desired_title))
        except Exception:
            # On failure assert_title gives us a better error message than the timeout.
            self.assert_title(desired_title)

    def assert_text_eventually(self, expected_text, target):
        try:
            element_locator = (By.ID, target)
            WebDriverWait(self.driver, 15).until(
                lambda d: d.find_element(*element_locator).text == expected_text
            )
        except Exception as e:
            # On failure assert_text gives us a better error message than the timeout.
            self.assert_text(expected_text, target)

    def assert_text_eventually_contains(self, expected_text, target):
        element_locator = (By.ID, target)
        WebDriverWait(self.driver, 15).until(
            lambda d: expected_text in d.find_element(*element_locator).text
        )

    def click_button_eventually(self, target):
        element_locator = (By.ID, target)
        WebDriverWait(self.driver, 60).until(
            lambda d: d.find_element(*element_locator) != None
        )
        return self.click(f"button#{target}")