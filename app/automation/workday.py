from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from app.services.applications import question_key, workday_host

FIELDS_SCRIPT = """() => {
    const visible = element => (element.type === 'file' || element.getClientRects().length) &&
        !element.disabled &&
        !element.closest('[aria-hidden="true"]') &&
        element.getAttribute('data-automation-id') !== 'beecatcher';
    const labelText = node => {
        if (!node) return '';
        const copy = node.cloneNode(true);
        copy.querySelectorAll('input, textarea, select, button, [role="combobox"]')
            .forEach(control => control.remove());
        return copy.textContent.replace(/\\s+/g, ' ').trim();
    };
    const controls = [...document.querySelectorAll(
        'input, textarea, select, button[aria-haspopup="listbox"], [role="combobox"]'
    )].filter(element => visible(element) && (
        element.matches('button[aria-haspopup="listbox"], [role="combobox"]') ||
        !['hidden', 'submit', 'button', 'search'].includes(element.type)));
    return [...new Set(controls)].map((element, index) => {
        const container = element.closest('[data-automation-id^="formField"], fieldset');
        const label = labelText(element.labels?.[0]) || element.getAttribute('aria-label') ||
            labelText(document.getElementById(element.getAttribute('aria-labelledby'))) ||
            labelText(container?.querySelector('label, legend')) ||
            element.getAttribute('placeholder') || element.name || element.id;
        element.setAttribute('data-workfinder-field', String(index + 1));
        let type = element.type || element.getAttribute('role') || 'text';
        if (element.tagName === 'BUTTON') type = 'combobox';
        let value = element.tagName === 'BUTTON' ? element.innerText :
            ('value' in element ? element.value : element.innerText || '');
        if (['checkbox', 'radio'].includes(type)) value = element.checked ? 'yes' : 'no';
        if (type === 'file') value = [...element.files].map(file => file.name).join(', ');
        if (type === 'password') value = value ? '••••' : '';
        return {id: index + 1, label: (label || 'Field ' + (index + 1)).trim(), type,
            value: String(value).trim(),
            required: element.required || element.getAttribute('aria-required') === 'true',
            options: element.tagName === 'SELECT'
                ? [...element.options].map(option => ({label: option.text, value: option.value}))
                : []};
    });
}"""

PROFILE_LABELS = {
    "first_name": ("first name", "given name", "legal first name", "imię"),
    "last_name": ("last name", "family name", "surname", "legal last name", "nazwisko"),
    "email": ("email", "email address", "e-mail", "e-mail address"),
    "phone": ("phone number", "telephone", "mobile number", "numer telefonu"),
    "address": ("address line 1", "street address", "street", "adres"),
    "city": ("city", "town", "miasto"),
    "postal_code": ("postal code", "zip code", "kod pocztowy"),
    "country": ("country", "country/territory", "kraj"),
    "linkedin": ("linkedin", "linkedin profile", "linkedin url"),
}
CONFIRMED = re.compile(
    r"thank you for (?:applying|your application)|"
    r"(?:application|application was|application has been) successfully submitted|"
    r"application (?:was|has been) submitted|you have successfully applied",
    re.I,
)
NEXT_LABELS = {"sign in", "continue", "next", "save and continue", "create account"}
NEXT_SELECTOR = (
    '[data-automation-id="bottom-navigation-next-button"], '
    '[data-automation-id="pageFooterNextButton"], '
    '[data-automation-id="pageFooterSubmitButton"]'
)
BACK_SELECTOR = (
    '[data-automation-id="bottom-navigation-back-button"], '
    '[data-automation-id="pageFooterPreviousButton"], '
    '[data-automation-id="pageFooterBackButton"]'
)


class JobUnavailable(ValueError):
    """The employer explicitly reports that the requested posting is unavailable."""


def profile_value(label: str, profile: dict) -> str | None:
    normalized = question_key(label)
    saved = profile.get("answers", {}).get(normalized)
    if saved is not None:
        return saved
    for key, labels in PROFILE_LABELS.items():
        if normalized in labels:
            return profile.get(key) or None
    return None


def fingerprint(snapshot: dict) -> str:
    content = {key: snapshot.get(key) for key in ("url", "text", "fields", "submit")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class WorkdayForm:
    """One live application. Only an explicit send action can click final Submit."""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.host = ""
        self.storage_path = None
        self.uploaded = False
        self.autofilled = set()
        self.requisition = ""
        self.cv_name = ""

    async def start(self, job, profile: dict, cv: dict, directory: Path) -> dict:  # noqa: ANN001
        self.host = workday_host(job)
        if not self.host:
            raise ValueError("Nieobsługiwany adres formularza.")
        self.requisition = urlsplit(job.original_url).path.rsplit("_", 1)[-1]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.storage_path = directory / (self.host + ".json")
        self.playwright = await async_playwright().start()
        browser_env = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR")
            if key in os.environ
        }
        self.browser = await self.playwright.chromium.launch(
            headless=True, chromium_sandbox=True, env=browser_env,
        )
        self.context = await self.browser.new_context(
            storage_state=self.storage_path if self.storage_path.exists() else None,
            accept_downloads=False,
            service_workers="block",
        )
        await self.context.route("**/*", self.route)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(15000)
        await self.page.goto(job.original_url, wait_until="domcontentloaded", timeout=45000)
        apply_button = self.page.get_by_role("button", name="Apply", exact=True).first
        unavailable = self.page.get_by_text(
            "The page you are looking for doesn't exist.", exact=True
        )
        await apply_button.or_(unavailable).first.wait_for(timeout=30000)
        if await unavailable.count() and await unavailable.first.is_visible():
            raise JobUnavailable("Ogłoszenie jest już niedostępne na stronie pracodawcy.")
        if self.requisition not in urlsplit(self.page.url).path:
            raise ValueError("Strona pracodawcy przekierowała poza wybraną ofertę.")
        decline = self.page.get_by_role("button", name="Decline", exact=True)
        if await decline.count() == 1 and await decline.is_visible():
            await decline.click()
        await apply_button.click()
        # The modal may offer several application methods or immediately require sign-in.
        methods = self.page.get_by_role("button", name="Autofill with Resume", exact=True)
        manual = self.page.get_by_role("button", name="Apply Manually", exact=True)
        with suppress(Exception):
            await methods.or_(manual).first.wait_for(timeout=5000)
        if await methods.count() and await methods.first.is_visible():
            await methods.first.click()
        elif await manual.count() and await manual.first.is_visible():
            await manual.first.click()
        return await self.inspect(profile, cv)

    async def route(self, route) -> None:  # noqa: ANN001
        parsed = urlsplit(route.request.url)
        host = parsed.hostname or ""
        allowed = host == self.host or any(
            host.endswith(suffix)
            for suffix in (
                ".workday.com",
                ".myworkday.com",
                ".myworkdaycdn.com",
                ".workdaycdn.com",
                ".myworkdayjobs.com",
            )
        )
        if parsed.scheme != "https" or not allowed:
            await route.abort()
        else:
            await route.continue_()

    async def settle(self) -> None:
        with suppress(Exception):
            await self.page.wait_for_load_state("networkidle", timeout=5000)
        if urlsplit(self.page.url).hostname != self.host:
            raise ValueError("Formularz przekierował poza obsługiwaną stronę pracodawcy.")

    async def submit_button(self):  # noqa: ANN201
        button = self.page.locator(NEXT_SELECTOR)
        if await button.count() and await button.first.is_visible():
            label = (await button.first.inner_text()).strip()
            if label.casefold() in {"submit", "submit application"}:
                return button.first
        return None

    async def next_button(self):  # noqa: ANN201
        navigation = self.page.locator(NEXT_SELECTOR)
        if await navigation.count() == 1:
            return navigation
        authentication = self.page.locator(
            'form[data-automation-id="signInFormo"] button[type="submit"]'
        )
        if await authentication.count() == 1:
            return authentication
        main = self.page.locator("main")
        root = main if await main.count() == 1 else self.page
        return root.get_by_role(
            "button",
            name=re.compile(r"^(Sign In|Continue|Next|Save and Continue|Create Account)$", re.I),
        )

    async def inspect(self, profile: dict, cv: dict, *, autofill: bool = True) -> dict:
        await self.settle()
        if "/apply/" in self.page.url:
            ready = self.page.locator(
                'input:not([type="hidden"]):not([type="search"]):visible, textarea:visible, '
                + NEXT_SELECTOR
                + ', input[type="file"]'
            )
            await ready.first.wait_for(state="attached", timeout=30000)
        notice = ""
        uploads = self.page.locator('input[type="file"]')
        if cv and not self.uploaded and await uploads.count():
            # Workday's CV uploader has this marker. Other uploaders need explicit handling.
            resume = self.page.locator('[data-automation-id="file-upload-input-ref"]')
            if await resume.count() == 1:
                await self.attach_cv(resume, cv)
                await self.settle()
        fields = await self.page.evaluate(FIELDS_SCRIPT)
        if autofill:
            for field in fields:
                field_key = (question_key(field["label"]), field["type"], field["id"])
                if field_key in self.autofilled:
                    continue
                self.autofilled.add(field_key)
                value = profile_value(field["label"], profile)
                remembered = question_key(field["label"]) in profile.get("answers", {})
                empty = field["value"].casefold() in {"", "select one", "select...", "select"}
                if value is not None and (empty or remembered) and field["type"] != "password":
                    with suppress(Exception):
                        await self.fill(field, value)
            fields = await self.page.evaluate(FIELDS_SCRIPT)
        if len(fields) > 60:
            raise ValueError("Formularz ma zbyt wiele pól na jedną stronę. Użyj strony pracodawcy.")
        body = self.page.locator("main")
        if await body.count() != 1:
            body = self.page.locator("body")
        text = (await body.inner_text()).strip()
        if len(text) > 64000:
            raise ValueError("Podsumowanie jest zbyt długie do sprawdzenia w Telegramie.")
        submit = await self.submit_button()
        next_button = await self.next_button()
        next_label = ""
        if not submit and await next_button.count() == 1 and await next_button.is_visible():
            next_label = (await next_button.inner_text()).strip()
        if await self.page.locator('iframe[src*="captcha"]').count():
            notice = "Strona wymaga CAPTCHA. Dokończ aplikację na stronie pracodawcy."
            next_label = ""
        if not fields and not next_label and not submit:
            notice = (
                notice
                or "Brak obsługiwanych pól. Możesz odświeżyć formularz "
                "lub dokończyć na stronie firmy."
            )
        snapshot = {
            "url": self.page.url,
            "text": text if submit else text[:9000],
            "fields": fields,
            "next": next_label,
            "submit": bool(submit),
            "notice": notice,
            "cv_attached": self.uploaded
            and (not submit or self.cv_name.casefold() in text.casefold()),
            "job_reference": self.requisition,
        }
        back = self.page.locator(BACK_SELECTOR)
        snapshot["back"] = await back.count() == 1 and await back.is_visible()
        create_account = self.page.locator('[data-automation-id="createAccountLink"]')
        snapshot["create_account"] = (
            await create_account.count() == 1 and await create_account.is_visible()
        )
        sign_in = self.page.locator('[data-automation-id="signInLink"]')
        snapshot["sign_in"] = await sign_in.count() == 1 and await sign_in.is_visible()
        if submit and not snapshot["cv_attached"]:
            snapshot["notice"] = "Załączenie CV wymaga sprawdzenia. Wróć do kroku z CV."
        snapshot["fingerprint"] = fingerprint(snapshot)
        await self.save_session()
        return snapshot

    async def fill(self, field: dict, value: str) -> None:
        locator = self.page.locator(f'[data-workfinder-field="{field["id"]}"]')
        if field["type"] in {"checkbox", "radio"}:
            if value.casefold() not in {"yes", "no"}:
                raise ValueError("Dla tego pola wybierz yes lub no.")
            await locator.set_checked(value.casefold() == "yes")
        elif field["options"]:
            choice = next(
                (
                    option["value"]
                    for option in field["options"]
                    if option["value"] == value or option["label"].casefold() == value.casefold()
                ),
                value,
            )
            await locator.select_option(value=choice)
        elif field["type"] in {"combobox", "select-one"}:
            await locator.click()
            await self.page.get_by_role("option", name=value, exact=True).click()
        else:
            await locator.fill(value)
        await locator.press("Tab")

    async def attach_cv(self, locator, cv: dict) -> None:  # noqa: ANN001
        data = Path(cv["path"]).read_bytes()
        if cv.get("sha256") and hashlib.sha256(data).hexdigest() != cv["sha256"]:
            raise ValueError("Plik CV zmienił się. Przygotuj aplikację ponownie.")
        await locator.set_input_files(
            {
                "name": cv["name"],
                "mimeType": "application/pdf",
                "buffer": data,
            }
        )
        self.uploaded = True
        self.cv_name = cv["name"]

    async def upload_cv(self, field: dict, cv: dict) -> None:
        locator = self.page.locator(f'[data-workfinder-field="{field["id"]}"]')
        await self.attach_cv(locator, cv)

    async def options(self, field: dict) -> list[dict]:
        if field["options"]:
            return field["options"]
        if field["type"] in {"checkbox", "radio"}:
            return [{"label": "Tak", "value": "yes"}, {"label": "Nie", "value": "no"}]
        locator = self.page.locator(f'[data-workfinder-field="{field["id"]}"]')
        await locator.click()
        options = self.page.get_by_role("option")
        with suppress(Exception):
            await options.first.wait_for(timeout=3000)
        labels = await options.all_inner_texts()
        return [
            {"label": label.strip(), "value": label.strip()} for label in labels if label.strip()
        ]

    async def advance(self) -> None:
        if await self.submit_button():
            raise ValueError("Oczekiwanie na zatwierdzenie aplikacji.")
        button = await self.next_button()
        if await button.count() != 1:
            raise ValueError("Nie znaleziono jednoznacznego przycisku przejścia dalej.")
        label = (await button.inner_text()).strip().casefold()
        if label not in NEXT_LABELS:
            raise ValueError("Ten przycisk wymaga obsługi na stronie pracodawcy.")
        await button.click()

    async def back(self) -> None:
        button = self.page.locator(BACK_SELECTOR)
        if await button.count() != 1:
            raise ValueError("Przycisk Wstecz jest niedostępny.")
        await button.click()

    async def open_account(self) -> None:
        await self.page.locator('[data-automation-id="createAccountLink"]').click()

    async def open_sign_in(self) -> None:
        await self.page.locator('[data-automation-id="signInLink"]').click()

    async def validate_review(self, approved: dict, profile: dict, cv: dict) -> dict:
        current = await self.inspect(profile, cv, autofill=False)
        if self.requisition not in urlsplit(self.page.url).path:
            raise ValueError("Adres formularza nie odpowiada wybranej ofercie.")
        if not current["submit"] or current["fingerprint"] != approved.get("fingerprint"):
            raise ValueError("Formularz zmienił się. Sprawdź aktualne podsumowanie przez /form.")
        if not current["cv_attached"]:
            raise ValueError("Nie potwierdzono załączenia CV. Użyj strony pracodawcy.")
        if any(
            field["required"]
            and (not field["value"] or field["type"] == "checkbox" and field["value"] != "yes")
            for field in current["fields"]
        ):
            raise ValueError("Formularz ma niewypełnione wymagane pola.")
        return current

    async def submit(self) -> str:
        button = await self.submit_button()
        if button is None:
            raise ValueError("Przycisk wysłania jest już niedostępny.")
        await button.click()
        await self.page.get_by_text(CONFIRMED).first.wait_for(timeout=45000)
        await self.settle()
        if await self.submit_button():
            raise ValueError("Formularz nadal pokazuje przycisk wysłania.")
        confirmation = (await self.page.get_by_text(CONFIRMED).first.inner_text()).strip()
        await self.save_session()
        return self.page.url + "\n" + confirmation[:1500]

    async def save_session(self) -> None:
        if self.storage_path:
            state = await self.context.storage_state(indexed_db=True)
            temporary = self.storage_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.storage_path)

    async def close(self) -> None:
        for resource in (self.context, self.browser):
            if resource:
                with suppress(Exception):
                    await resource.close()
        if self.playwright:
            await self.playwright.stop()
