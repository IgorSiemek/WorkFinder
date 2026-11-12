"""Exercise the application browser against an isolated employer-form fixture."""

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.automation.workday import JobUnavailable, WorkdayForm

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1",
    reason="Set RUN_BROWSER_TESTS=1 after installing Chromium",
)

FORM_HTML = """<!doctype html><html lang="en"><body><main>
<h1>Finance Intern</h1><button onclick="method()">Apply</button></main>
<script>
window.submissions = 0;
let applicant = {};
function method() {
 document.querySelector('main').innerHTML =
  '<button onclick="information()">Autofill with Resume</button>';
}
function information() {
 document.querySelector('main').innerHTML = `<h2>My Information</h2>
 <label>First Name<input required id="first"></label>
 <label>Last Name<input required id="last"></label>
 <label>Email Address<input required type="email" id="email"></label>
 <label>Phone Number<input required id="phone"></label>
 <input type="file" data-automation-id="file-upload-input-ref" id="cv">
 <button data-automation-id="bottom-navigation-next-button" onclick="questions()">
 Save and Continue</button>`;
}
function questions() {
 applicant.first = document.querySelector('#first').value;
 applicant.last = document.querySelector('#last').value;
 applicant.email = document.querySelector('#email').value;
 applicant.phone = document.querySelector('#phone').value;
 applicant.cv = document.querySelector('#cv').files[0]?.name || '';
 document.querySelector('main').innerHTML = `<h2>Application Questions</h2>
 <label>Work authorization<select required id="authorized">
 <option value="">Select One</option><option value="yes">Yes</option>
 <option value="no">No</option></select></label>
 <label>I confirm these answers<input required type="checkbox" id="consent"></label>
 <button data-automation-id="bottom-navigation-next-button" onclick="review()">
 Save and Continue</button>`;
}
function review() {
 applicant.authorization = document.querySelector('#authorized').value;
 applicant.consent = document.querySelector('#consent').checked;
 if (!applicant.authorization || !applicant.consent) return;
 document.querySelector('main').innerHTML = `<h2>Review</h2><p id="review-data"></p>
 <button data-automation-id="bottom-navigation-next-button" onclick="submitApplication()">
 Submit</button>`;
 document.querySelector('#review-data').innerText = Object.values(applicant).join(' / ');
}
function submitApplication() {
 window.submissions++;
 document.querySelector('main').innerHTML = '<h2>Thank you for applying</h2>';
}
</script></body></html>"""


class FixtureForm(WorkdayForm):
    form_html = FORM_HTML

    async def route(self, route):
        await route.fulfill(status=200, content_type="text/html", body=self.form_html)


def job():
    return SimpleNamespace(
        source="Motorola Solutions Careers",
        company="Motorola Solutions",
        original_url="https://motorolasolutions.wd5.myworkdayjobs.com/en-US/Careers/job/Krakow/Finance-Intern_R1",
    )


@pytest.mark.parametrize("footer", ["bottom-navigation-next-button", "pageFooterNextButton"])
def test_browser_fills_cv_profile_questions_and_waits_for_explicit_submission(tmp_path, footer):
    async def exercise():
        cv_path = tmp_path / "cv.pdf"
        cv_path.write_bytes(b"%PDF-1.7\n%%EOF")
        cv = {"path": str(cv_path), "name": "Anna CV.pdf"}
        profile = {
            "first_name": "Anna",
            "last_name": "Example",
            "email": "anna@example.com",
            "phone": "+48123456789",
        }
        form = FixtureForm()
        form.form_html = FORM_HTML.replace("bottom-navigation-next-button", footer)
        try:
            snapshot = await form.start(job(), profile, cv, tmp_path / "sessions")
            assert snapshot["cv_attached"]
            values = [field["value"] for field in snapshot["fields"] if field["type"] != "file"]
            assert values == list(profile.values())
            assert await form.page.evaluate("window.submissions") == 0
            await form.advance()
            snapshot = await form.inspect(profile, cv)
            authorization, consent = snapshot["fields"]
            assert await form.options(authorization) == [
                {"label": "Select One", "value": ""},
                {"label": "Yes", "value": "yes"},
                {"label": "No", "value": "no"},
            ]
            await form.fill(authorization, "yes")
            await form.fill(consent, "yes")
            await form.advance()
            snapshot = await form.inspect(profile, cv)
            assert snapshot["submit"] and "Anna CV.pdf" in snapshot["text"]
            assert await form.page.evaluate("window.submissions") == 0
            with pytest.raises(ValueError):
                await form.advance()
            await form.validate_review(snapshot, profile, cv)
            receipt = await form.submit()
            assert "Thank you for applying" in receipt
            assert await form.page.evaluate("window.submissions") == 1
        finally:
            await form.close()

    asyncio.run(exercise())


def test_review_change_prevents_submission_and_password_never_enters_snapshot(tmp_path):
    async def exercise():
        form = FixtureForm()
        cv = tmp_path / "cv.pdf"
        cv.write_bytes(b"%PDF-1.7\n%%EOF")
        document = {"path": str(cv), "name": "CV.pdf"}
        try:
            await form.start(job(), {}, document, tmp_path / "sessions")
            await form.page.locator("main").evaluate(
                "element => element.innerHTML = '<h2>Review</h2>' +"
                "'<label>Password<input type=password value=secret></label>' +"
                "'<button data-automation-id=bottom-navigation-next-button>Submit</button>'"
            )
            snapshot = await form.inspect({}, document)
            assert "secret" not in str(snapshot)
            await form.page.locator("h2").evaluate(
                "element => element.innerText = 'Changed Review'"
            )
            with pytest.raises(ValueError, match="zmienił"):
                await form.validate_review(snapshot, {}, document)
            assert await form.page.evaluate("window.submissions") == 0
        finally:
            await form.close()

    asyncio.run(exercise())


def test_saved_answers_fill_once_and_user_edits_are_preserved(tmp_path):
    async def exercise():
        form = FixtureForm()
        cv = tmp_path / "cv.pdf"
        cv.write_bytes(b"%PDF-1.7\n%%EOF")
        document = {"path": str(cv), "name": "CV.pdf"}
        profile = {"answers": {"work authorization": "yes"}}
        try:
            await form.start(job(), profile, document, tmp_path / "sessions")
            await form.advance()
            snapshot = await form.inspect(profile, document)
            authorization = snapshot["fields"][0]
            assert authorization["value"] == "yes"
            await form.fill(authorization, "no")
            snapshot = await form.inspect(profile, document)
            assert snapshot["fields"][0]["value"] == "no"
        finally:
            await form.close()

    asyncio.run(exercise())


def test_account_form_uses_primary_button_and_omits_honeypot(tmp_path):
    async def exercise():
        form = FixtureForm()
        try:
            await form.start(job(), {}, {}, tmp_path / "sessions")
            await form.page.locator("body").evaluate("""element => element.innerHTML = `
                <button data-automation-id="utilityButtonSignIn">Sign In</button>
                <main><form data-automation-id="signInFormo">
                <label>Email Address*<input required></label>
                <label>Password*<input required type=password></label>
                <button type=submit data-automation-id="createAccountSubmitButton">
                Create Account</button></form>
                <button data-automation-id="signInLink">Sign In</button>
                <label>Website<input data-automation-id="beecatcher"></label></main>`""")
            snapshot = await form.inspect({}, {})
            assert snapshot["next"] == "Create Account"
            assert snapshot["sign_in"]
            assert len(snapshot["fields"]) == 2
        finally:
            await form.close()

    asyncio.run(exercise())


def test_withdrawn_employer_posting_is_reported_before_opening_an_application(tmp_path):
    class WithdrawnForm(WorkdayForm):
        async def route(self, route):
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="<main>The page you are looking for doesn't exist.</main>",
            )

    async def exercise():
        form = WithdrawnForm()
        try:
            with pytest.raises(JobUnavailable):
                await form.start(job(), {}, {}, tmp_path / "sessions")
        finally:
            await form.close()

    asyncio.run(exercise())
