from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from uuid import uuid4

import httpx

from .errors import LoginError
from .models import (
    ConsumptionComparison,
    ConsumptionForecast,
    CurrentConsumption,
    MeterReading,
    Period,
    Reading,
    ReadingKind,
    RoomConsumption,
)


def _default_headers() -> dict[str, str]:
    # A realistic UA helps when portals gate non-browser clients.
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


@dataclass(frozen=True)
class LoginAttemptInfo:
    login_page_url: str
    credential_batch_url: str
    sap_client: str


class BrunataClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        sap_client: str = "201",
        sap_language: str = "DE",
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.sap_client = sap_client
        self.sap_language = sap_language
        self._timeout = httpx.Timeout(timeout_s)
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers=_default_headers(),
            follow_redirects=True,
        )
        self._logged_in = False
        self._last_login_attempt: LoginAttemptInfo | None = None
        self._csrf_tokens: dict[str, str] = {}
        self._user_unit_id: str | None = None
        self._contact_person: str | None = None
        self._user_context: dict[str, Any] | None = None
        self._basic_auth: httpx.BasicAuth | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BrunataClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    @property
    def last_login_attempt(self) -> LoginAttemptInfo | None:
        return self._last_login_attempt

    def _abs(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _service_root(self, service: str) -> str:
        # Always include trailing slash (SAP OData service root).
        return self._abs(f"/sap/opu/odata/bme/{service.strip('/')}/")

    def _service_batch_url(self, service: str) -> str:
        return self._service_root(service) + "$batch?" + f"sap-client={quote(self.sap_client)}"

    def _service_head_url(self, service: str) -> str:
        return self._service_root(service) + "?" + f"sap-client={quote(self.sap_client)}"

    def _referer_login(self) -> str:
        return self._abs(f"/np_anmeldung/index.html?sap-language={quote(self.sap_language)}")

    def _referer_services(self) -> str:
        # After login the UI navigates into /np_dienste.
        return self._abs(f"/np_dienste/index.html?sap-language={quote(self.sap_language)}")

    def _odata_headers(
        self,
        *,
        referer: str,
        csrf_token: str | None = None,
        user_unit_id: str | None = None,
        contact_person: str | None = None,
        cancel_on_close: bool = True,
    ) -> dict[str, str]:
        h: dict[str, str] = {
            "Accept-Language": "de",
            "DataServiceVersion": "2.0",
            "MaxDataServiceVersion": "2.0",
            "X-Requested-With": "XMLHttpRequest",
            "sap-contextid-accept": "header",
            "sap-cancel-on-close": "true" if cancel_on_close else "false",
            "Origin": self.base_url,
            "Referer": referer,
        }
        if csrf_token is not None:
            h["x-csrf-token"] = csrf_token
        if user_unit_id is not None:
            h["UserUnitID"] = user_unit_id
        if contact_person is not None:
            h["ContactPerson"] = contact_person
        return h

    async def _fetch_csrf_token(self, service: str, *, referer: str) -> str:
        cached = self._csrf_tokens.get(service)
        if cached:
            return cached

        r = await self._client.head(
            self._service_head_url(service),
            auth=self._basic_auth,
            headers={
                **self._odata_headers(referer=referer),
                "Content-Type": "application/json",
                "x-csrf-token": "Fetch",
            },
        )
        token = r.headers.get("x-csrf-token")
        if not token:
            loc = r.headers.get("location") or ""
            snippet = (r.text or "")[:200].replace("\n", "\\n")
            cookie_names = sorted({c.name for c in self._client.cookies.jar})
            raise LoginError(
                f"Missing x-csrf-token from {service} HEAD response "
                f"(status={r.status_code}, location={loc!r}, "
                f"cookies={cookie_names!r}, body_snippet={snippet!r})."
            )
        self._csrf_tokens[service] = token
        return token

    @staticmethod
    def _build_batch_get(*, boundary: str, relative_get: str, extra_headers: dict[str, str]) -> str:
        headers = "".join(f"{k}: {v}\r\n" for k, v in extra_headers.items())
        return (
            f"\r\n--{boundary}\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            f"GET {relative_get} HTTP/1.1\r\n"
            f"{headers}\r\n"
            f"\r\n--{boundary}--\r\n"
        )

    @staticmethod
    def _extract_json_objects(text: str) -> list[dict[str, Any]]:
        """
        Extract JSON payload(s) from SAP $batch multipart responses.
        We don't try to fully parse multipart; we just decode JSON objects found inside.
        """
        dec = json.JSONDecoder()
        out: list[dict[str, Any]] = []
        i = 0
        while True:
            start = text.find("{", i)
            if start == -1:
                return out
            try:
                obj, end = dec.raw_decode(text[start:])
            except json.JSONDecodeError:
                i = start + 1
                continue
            if isinstance(obj, dict):
                out.append(obj)
            i = start + end

    @staticmethod
    def _sap_date_to_datetime(value: str) -> datetime | None:
        # SAP uses strings like "/Date(1767139200000)/"
        m = re.search(r"/Date\((?P<ms>-?\d+)\)/", value)
        if not m:
            return None
        ms = int(m.group("ms"))
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)

    async def _post_batch(
        self, service: str, *, body: str, headers: dict[str, str]
    ) -> httpx.Response:
        return await self._client.post(
            self._service_batch_url(service),
            auth=self._basic_auth,
            content=body.encode("utf-8"),
            headers=headers,
        )

    async def login(self) -> None:
        # Munich portal login is SAPUI5 + SAP OData (not ASP.NET form login).
        login_page_url = self._referer_login()
        r = await self._client.get(login_page_url)
        if r.status_code >= 400:
            raise LoginError(f"Failed to load login UI: {login_page_url} ({r.status_code}).")

        # Prime logon service similarly to browser flow:
        # - HEAD (x-csrf-token: Fetch)
        # - $batch GET InfoTextSet('BME_NP_REG_LOGON_START')
        # - HEAD again
        for _ in range(2):
            await self._client.head(
                self._service_head_url("NP_REG_LOGON_SRV_01"),
                headers={
                    **self._odata_headers(referer=login_page_url),
                    "Content-Type": "application/json",
                    "x-csrf-token": "Fetch",
                },
            )

        start_boundary = f"batch_{uuid4().hex[:4]}-{uuid4().hex[:4]}-{uuid4().hex[:4]}"
        start_rel = f"InfoTextSet('BME_NP_REG_LOGON_START')?sap-client={quote(self.sap_client)}"
        start_body = self._build_batch_get(
            boundary=start_boundary,
            relative_get=start_rel,
            extra_headers={
                "sap-cancel-on-close": "true",
                "X-Requested-With": "XMLHttpRequest",
                "sap-contextid-accept": "header",
                "Accept": "application/json",
                "Accept-Language": "de",
                "DataServiceVersion": "2.0",
                "MaxDataServiceVersion": "2.0",
            },
        )
        start_headers = {
            **self._odata_headers(referer=login_page_url),
            "Accept": "multipart/mixed",
            "Content-Type": f"multipart/mixed;boundary={start_boundary}",
        }
        start_resp = await self._post_batch(
            "NP_REG_LOGON_SRV_01",
            body=start_body,
            headers=start_headers,
        )
        if start_resp.status_code != 202:
            raise LoginError(f"Logon start batch failed: HTTP {start_resp.status_code}")

        await self._client.head(
            self._service_head_url("NP_REG_LOGON_SRV_01"),
            headers={
                **self._odata_headers(referer=login_page_url),
                "Content-Type": "application/json",
                "x-csrf-token": "Fetch",
            },
        )

        # Submit credentials via $batch -> CredentialSet.
        boundary = f"batch_{uuid4().hex[:4]}-{uuid4().hex[:4]}-{uuid4().hex[:4]}"
        changeset = f"changeset_{uuid4().hex[:4]}-{uuid4().hex[:4]}-{uuid4().hex[:4]}"
        password_b64 = base64.b64encode(self.password.encode("utf-8")).decode("ascii")
        payload = {"Action": "validLCR", "Email": self.username, "Password": password_b64}
        payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        content_id = f"id-{uuid4().int % 10**13}-1"

        body = (
            f"\r\n--{boundary}\r\n"
            f"Content-Type: multipart/mixed; boundary={changeset}\r\n\r\n"
            f"--{changeset}\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            f"POST CredentialSet?sap-client={self.sap_client} HTTP/1.1\r\n"
            "X-Requested-With: XMLHttpRequest\r\n"
            "sap-contextid-accept: header\r\n"
            "Accept: application/json\r\n"
            "Accept-Language: de\r\n"
            "DataServiceVersion: 2.0\r\n"
            "MaxDataServiceVersion: 2.0\r\n"
            "Content-Type: application/json\r\n"
            f"Content-ID: {content_id}\r\n"
            f"Content-Length: {len(payload_json.encode('utf-8'))}\r\n\r\n"
            f"{payload_json}\r\n"
            f"--{changeset}--\r\n\r\n"
            f"--{boundary}--\r\n"
        )

        batch_url = self._service_batch_url("NP_REG_LOGON_SRV_01")
        self._last_login_attempt = LoginAttemptInfo(
            login_page_url=login_page_url,
            credential_batch_url=batch_url,
            sap_client=self.sap_client,
        )

        headers = {
            **self._odata_headers(referer=login_page_url, cancel_on_close=False),
            "Accept": "multipart/mixed",
            "Content-Type": f"multipart/mixed;boundary={boundary}",
        }
        resp = await self._post_batch("NP_REG_LOGON_SRV_01", body=body, headers=headers)
        if resp.status_code != 202:
            raise LoginError(f"Credential batch failed: HTTP {resp.status_code}")
        status_line = re.search(
            r"HTTP/1\\.1\\s+(?P<code>\\d{3})\\s+(?P<msg>[^\\r\\n]+)",
            resp.text or "",
        )
        if status_line:
            code = int(status_line.group("code"))
            msg = status_line.group("msg")
            if code >= 400:
                raise LoginError(f"CredentialSet inner response failed: {code} {msg}")
        for o in self._extract_json_objects(resp.text or ""):
            if "error" in o:
                raise LoginError("CredentialSet returned an error payload.")

        # Extract and configure SAP Basic auth for subsequent OData calls.
        # The CredentialSet payload returns the SAP user password base64-encoded.
        for o in self._extract_json_objects(resp.text or ""):
            d = o.get("d")
            if not isinstance(d, dict):
                continue
            user_id = d.get("Userid")
            pw_b64 = d.get("Password")
            if isinstance(user_id, str) and isinstance(pw_b64, str) and user_id:
                try:
                    pw_plain = base64.b64decode(pw_b64, validate=True).decode("utf-8")
                except Exception as e:  # noqa: BLE001
                    raise LoginError(
                        "Failed to decode SAP basic-auth password from CredentialSet."
                    ) from e
                self._basic_auth = httpx.BasicAuth(user_id, pw_plain)
                break

        # The CredentialSet response includes a Serviceurl that the UI navigates to next.
        # Following it is required to establish the authenticated session for later OData calls.
        service_url: str | None = None
        for o in self._extract_json_objects(resp.text or ""):
            d = o.get("d")
            if isinstance(d, dict) and isinstance(d.get("Serviceurl"), str):
                service_url = d["Serviceurl"]
                break
        if service_url:
            if "://" in service_url:
                next_url = service_url
            elif service_url.startswith("/"):
                next_url = urljoin(self.base_url + "/", service_url.lstrip("/"))
            elif re.fullmatch(r"[A-Za-z0-9.-]+/.+", service_url):
                # Some instances return a host+path without scheme,
                # e.g. "nutzerportal.../np_dienste".
                next_url = "https://" + service_url
            else:
                next_url = urljoin(self.base_url + "/", service_url)
            r_next = await self._client.get(next_url, headers={"Referer": login_page_url})
            if r_next.status_code >= 400:
                raise LoginError(f"Serviceurl navigation failed: HTTP {r_next.status_code}")

        # Navigate into services area (matches browser flow and helps session establishment).
        r2a = await self._client.get(self._abs("/np_dienste"))
        if r2a.status_code >= 400:
            raise LoginError(f"Failed to load /np_dienste after login: HTTP {r2a.status_code}")
        r2b = await self._client.get(self._referer_services())
        if r2b.status_code >= 400:
            raise LoginError(f"Failed to load services UI after login: HTTP {r2b.status_code}")

        # Fetch user context via app launcher service (yields UserUnitID and Partner/ContactPerson).
        csrf = await self._fetch_csrf_token("NP_APPLAUNCHER_SRV", referer=self._referer_services())
        batch_boundary = f"batch_{uuid4().hex[:4]}-{uuid4().hex[:4]}-{uuid4().hex[:4]}"
        rel = f"UserContextSet?sap-client={quote(self.sap_client)}&$expand=Roles"
        batch_body = self._build_batch_get(
            boundary=batch_boundary,
            relative_get=rel,
            extra_headers={
                "sap-cancel-on-close": "true",
                "sap-contextid-accept": "header",
                "Accept": "application/json",
                "Accept-Language": "de",
                "DataServiceVersion": "2.0",
                "MaxDataServiceVersion": "2.0",
                "X-Requested-With": "XMLHttpRequest",
                "x-csrf-token": csrf,
            },
        )
        hdrs = {
            **self._odata_headers(referer=self._referer_services(), csrf_token=csrf),
            "Accept": "multipart/mixed",
            "Content-Type": f"multipart/mixed;boundary={batch_boundary}",
        }
        ctx_resp = await self._post_batch("NP_APPLAUNCHER_SRV", body=batch_body, headers=hdrs)
        if ctx_resp.status_code != 202:
            raise LoginError(f"UserContext batch failed: HTTP {ctx_resp.status_code}")

        objs = self._extract_json_objects(ctx_resp.text or "")
        ctx = None
        for o in objs:
            if "d" in o and isinstance(o["d"], dict) and "results" in o["d"]:
                ctx = o
                break
        if not ctx:
            raise LoginError("Could not parse UserContextSet response.")

        results = ctx["d"].get("results") or []
        if not results:
            raise LoginError("Empty UserContextSet results (login likely failed).")

        first = results[0]
        self._user_unit_id = str(first.get("UserUnitID") or "")
        self._contact_person = str(first.get("Partner") or "")
        if not self._user_unit_id or not self._contact_person:
            raise LoginError("UserContextSet missing UserUnitID/Partner.")
        self._user_context = first
        self._logged_in = True

    async def fetch(self, path_or_url: str) -> httpx.Response:
        url = path_or_url
        if not urlparse(url).scheme:
            url = self._abs(path_or_url)
        return await self._client.get(url)

    def user_unit_id(self) -> str | None:
        return self._user_unit_id

    def contact_person(self) -> str | None:
        return self._contact_person

    async def get_account(self) -> dict[str, Any]:
        if not self._logged_in:
            await self.login()
        return {
            "UserUnitID": self._user_unit_id,
            "ContactPerson": self._contact_person,
            "UserContext": self._user_context,
        }

    async def _dashboard_batch_get(self, relative_get: str) -> dict[str, Any]:
        if not self._logged_in:
            await self.login()
        if not self._user_unit_id or not self._contact_person:
            raise LoginError("Missing user context.")

        csrf = await self._fetch_csrf_token("NP_DASHBOARD_SRV", referer=self._referer_services())
        boundary = f"batch_{uuid4().hex[:4]}-{uuid4().hex[:4]}-{uuid4().hex[:4]}"
        body = self._build_batch_get(
            boundary=boundary,
            relative_get=relative_get,
            extra_headers={
                "sap-cancel-on-close": "true",
                "UserUnitID": self._user_unit_id,
                "ContactPerson": self._contact_person,
                "sap-contextid-accept": "header",
                "Accept": "application/json",
                "x-csrf-token": csrf,
                "Accept-Language": "de",
                "DataServiceVersion": "2.0",
                "MaxDataServiceVersion": "2.0",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        headers = {
            **self._odata_headers(
                referer=self._referer_services(),
                csrf_token=csrf,
                user_unit_id=self._user_unit_id,
                contact_person=self._contact_person,
            ),
            "Accept": "multipart/mixed",
            "Content-Type": f"multipart/mixed;boundary={boundary}",
        }
        r = await self._post_batch("NP_DASHBOARD_SRV", body=body, headers=headers)
        if r.status_code != 202:
            raise LoginError(f"Dashboard batch failed: HTTP {r.status_code}")
        objs = self._extract_json_objects(r.text or "")
        for o in objs:
            if "d" in o:
                return o
        raise LoginError("Dashboard batch response did not contain JSON data.")

    async def get_dashboard_dates(self) -> dict[str, Any]:
        if not self._logged_in:
            await self.login()
        if not self._user_unit_id:
            raise LoginError("Missing UserUnitID.")
        rel = (
            f"DatesSet?sap-client={quote(self.sap_client)}"
            f"&$expand=Units&$filter=Nutzein%20eq%20%27{quote(self._user_unit_id)}%27"
        )
        return await self._dashboard_batch_get(rel)

    async def get_periods(self) -> list[Period]:
        """
        Return all dashboard periods (e.g. calendar years) as start/end datetimes.

        Useful for integrations to show "which year" a value belongs to or to request
        a specific period via period_index. Values from meter/current consumption are
        per-period and typically reset when a new period (e.g. new year) starts.
        """
        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        out: list[Period] = []
        for p in results:
            if not isinstance(p, dict):
                continue
            ab_raw = p.get("Abdatum")
            bis_raw = p.get("Bisdatum")
            if not isinstance(ab_raw, str) or not isinstance(bis_raw, str):
                continue
            start = self._sap_date_to_datetime(ab_raw)
            end = self._sap_date_to_datetime(bis_raw)
            if start and end:
                out.append(Period(start=start, end=end))
        return out

    @staticmethod
    def _parse_sap_number(value: str) -> float:
        # Values may be padded with spaces and use dot decimals.
        return float(value.strip().replace(",", "."))

    async def get_meter_reading(
        self, *, cost_type: str, period_index: int = 0
    ) -> MeterReading:
        """
        Get the cumulative meter/index value (Zählerstand) for a cost type.

        Uses `NP_DASHBOARD_SRV/CumuConsumptionSet`. Values are per dashboard period
        (e.g. calendar year); they typically reset when a new period starts. Use
        get_periods() to list periods and period_index to request a specific one
        (0 = first, often the current period).
        """
        if not self._logged_in:
            await self.login()
        if not self._user_unit_id:
            raise LoginError("Missing UserUnitID.")

        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        periods_list = [p for p in results if isinstance(p, dict)]
        if not periods_list:
            raise LoginError("No dashboard periods found.")
        if period_index < 0 or period_index >= len(periods_list):
            raise LoginError(
                f"period_index {period_index} out of range (0..{len(periods_list) - 1})."
            )
        period = periods_list[period_index]

        bis_raw = period.get("Bisdatum")
        if not isinstance(bis_raw, str):
            raise LoginError("Missing Bisdatum in dashboard period.")

        bis_dt = self._sap_date_to_datetime(bis_raw)
        if not bis_dt:
            raise LoginError("Could not parse Bisdatum.")

        bis_filter_dt = bis_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            hours=23
        )
        bis_filter = bis_filter_dt.strftime("%Y-%m-%dT%H:%M:%S")

        rel = (
            f"CumuConsumptionSet?sap-client={quote(self.sap_client)}&$filter="
            f"Nutzein%20eq%20%27{quote(self._user_unit_id)}%27%20and%20"
            f"Bis%20eq%20datetime%27{quote(bis_filter)}%27%20and%20"
            f"Kotyp%20eq%20%27{quote(cost_type)}%27"
        )

        data = await self._dashboard_batch_get(rel)
        rows = (data.get("d") or {}).get("results") or []
        if not rows:
            raise LoginError(f"No CumuConsumptionSet results for cost type {cost_type}.")

        row = rows[0]
        val_raw = row.get("Verbrauch")
        if not isinstance(val_raw, str):
            raise LoginError("CumuConsumptionSet missing Verbrauch.")
        value = self._parse_sap_number(val_raw)
        unit = row.get("MassreadTxt") or row.get("Massread")
        unit_s = str(unit) if unit else None
        kind = ReadingKind.heating if cost_type.startswith("HZ") else ReadingKind.hot_water

        # CumuConsumptionSet uses the period end (Bis) as the timestamp.
        return MeterReading(
            timestamp=bis_dt,
            value=value,
            unit=unit_s,
            cost_type=cost_type,
            kind=kind,
        )

    async def get_meter_readings(
        self, *, period_index: int = 0
    ) -> dict[str, MeterReading]:
        """
        Return meter readings for *all* HZ.. and WW.. cost types in the given period.

        Output is keyed by `cost_type`. Values are per-period and may reset each year.
        Use period_index to select period (0 = first, e.g. current year).
        """
        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        periods_list = [p for p in results if isinstance(p, dict)]
        if not periods_list or period_index < 0 or period_index >= len(periods_list):
            return {}
        period = periods_list[period_index]

        unit_rows = ((period.get("Units") or {}).get("results") or [])
        all_cost_types = sorted(
            {
                r["CostType"]
                for r in unit_rows
                if isinstance(r, dict) and isinstance(r.get("CostType"), str)
            }
        )
        wanted = [ct for ct in all_cost_types if ct.startswith(("HZ", "WW"))]

        out: dict[str, MeterReading] = {}
        for ct in wanted:
            out[ct] = await self.get_meter_reading(
                cost_type=ct, period_index=period_index
            )
        return out

    async def get_supported_cost_types(self) -> dict[str, set[str]]:
        """
        Return cost types available per period.

        Output: { "<from_date_ms>": {"HZ01","WW01",...}, ... }
        """
        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        out: dict[str, set[str]] = {}
        for period in results:
            if not isinstance(period, dict):
                continue
            ab = period.get("Abdatum")
            key = str(ab) if ab is not None else "unknown"
            unit_rows = ((period.get("Units") or {}).get("results") or [])
            s: set[str] = set()
            for r in unit_rows:
                if isinstance(r, dict) and isinstance(r.get("CostType"), str):
                    s.add(r["CostType"])
            if s:
                out[key] = s
        return out

    async def get_monthly_consumption(
        self,
        *,
        cost_type: str,
        in_kwh: bool = True,
        period_index: int | None = None,
    ) -> list[Reading]:
        """
        Fetch monthly consumption series for a given CostType (e.g. HZ01, WW01).

        If period_index is None, tries all periods and returns the first with data.
        If set, only that period is queried (0 = first, e.g. current year).
        """
        kind = ReadingKind.heating if cost_type.startswith("HZ") else ReadingKind.hot_water
        if not self._logged_in:
            await self.login()
        if not self._user_unit_id:
            raise LoginError("Missing UserUnitID.")

        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        periods_list: list[dict[str, Any]] = [
            p for p in results if isinstance(p, dict)
        ]
        if period_index is not None:
            if period_index < 0 or period_index >= len(periods_list):
                return []
            periods_list = [periods_list[period_index]]

        for period in periods_list:
            bis_raw = period.get("Bisdatum")
            if not isinstance(bis_raw, str):
                continue
            bis_dt = self._sap_date_to_datetime(bis_raw)
            if not bis_dt:
                continue

            bis_filter_dt = bis_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                hours=23
            )
            bis_filter = bis_filter_dt.strftime("%Y-%m-%dT%H:%M:%S")

            rel = (
                f"CumuConsumptionMonSet?sap-client={quote(self.sap_client)}&$filter="
                f"Nutzein%20eq%20%27{quote(self._user_unit_id)}%27%20and%20"
                f"Bis%20eq%20datetime%27{quote(bis_filter)}%27%20and%20"
                f"Kotyp%20eq%20%27{quote(cost_type)}%27%20and%20"
                f"InKwh%20eq%20{'true' if in_kwh else 'false'}"
            )

            data = await self._dashboard_batch_get(rel)
            rows = (data.get("d") or {}).get("results") or []
            readings: list[Reading] = []
            for row in rows:
                dt = None
                for k in ("Datum", "Bis"):
                    v = row.get(k)
                    if isinstance(v, str):
                        dt = self._sap_date_to_datetime(v)
                        if dt:
                            break
                if not dt:
                    continue
                val_raw = row.get("Verbrauch")
                if val_raw is None:
                    continue
                try:
                    val = float(str(val_raw).replace(",", "."))
                except ValueError:
                    continue
                unit = row.get("MassreadTxt") or row.get("Massread")
                unit_s = str(unit) if unit else None
                readings.append(Reading(timestamp=dt, value=val, unit=unit_s, kind=kind))

            uniq: dict[tuple[datetime, float], Reading] = {}
            for r in readings:
                uniq[(r.timestamp, r.value)] = r
            if uniq:
                return sorted(uniq.values(), key=lambda r: r.timestamp)

        return []

    async def get_monthly_consumptions(
        self,
        kind: ReadingKind,
        *,
        in_kwh: bool = True,
        period_index: int = 0,
    ) -> dict[str, list[Reading]]:
        """
        Fetch monthly consumption series for *all* cost types matching `kind`.

        Output is keyed by `cost_type`. Use period_index to select period (0 = first).
        """
        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        periods_list = [p for p in results if isinstance(p, dict)]
        if not periods_list or period_index < 0 or period_index >= len(periods_list):
            return {}
        period = periods_list[period_index]

        unit_rows = ((period.get("Units") or {}).get("results") or [])
        all_cost_types = sorted(
            {
                r["CostType"]
                for r in unit_rows
                if isinstance(r, dict) and isinstance(r.get("CostType"), str)
            }
        )
        prefix = "HZ" if kind == ReadingKind.heating else "WW"
        wanted = [ct for ct in all_cost_types if ct.startswith(prefix)]

        out: dict[str, list[Reading]] = {}
        for ct in wanted:
            out[ct] = await self.get_monthly_consumption(
                cost_type=ct, in_kwh=in_kwh, period_index=period_index
            )
        return out

    async def get_current_consumption(
        self, kind: ReadingKind, *, period_index: int = 0
    ) -> CurrentConsumption:
        """
        Return the dashboard-style cumulative consumption (e.g. YTD) for the period.

        This is the sum of monthly kWh for the selected period, so it resets when a
        new period (e.g. new year) starts. Use period_index to select period
        (0 = first, often current year). as_of is the period end date.
        """
        if not self._logged_in:
            await self.login()

        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        periods_list = [p for p in results if isinstance(p, dict)]
        if not periods_list:
            raise LoginError("No dashboard period found.")
        if period_index < 0 or period_index >= len(periods_list):
            raise LoginError(
                f"period_index {period_index} out of range (0..{len(periods_list) - 1})."
            )
        period = periods_list[period_index]

        bis_raw = period.get("Bisdatum")
        if not isinstance(bis_raw, str):
            raise LoginError("Missing Bisdatum in dashboard period.")
        as_of = self._sap_date_to_datetime(bis_raw)
        if not as_of:
            raise LoginError("Could not parse Bisdatum.")

        unit_rows = ((period.get("Units") or {}).get("results") or [])
        cost_types = [
            r.get("CostType")
            for r in unit_rows
            if isinstance(r, dict) and isinstance(r.get("CostType"), str)
        ]

        if kind == ReadingKind.heating:
            cost_type = next((ct for ct in cost_types if ct.startswith("HZ")), "HZ01")
        else:
            cost_type = next((ct for ct in cost_types if ct.startswith("WW")), "WW01")

        monthly = await self.get_monthly_consumption(
            cost_type=cost_type, in_kwh=True, period_index=period_index
        )
        total = round(sum(r.value for r in monthly), 3)
        return CurrentConsumption(
            as_of=as_of,
            value=total,
            unit="kWh",
            kind=kind,
            cost_type=cost_type,
        )

    async def get_readings(self, kind: ReadingKind) -> list[Reading]:
        if not self._logged_in:
            await self.login()
        if not self._user_unit_id:
            raise LoginError("Missing UserUnitID.")

        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        if not results:
            return []
        # Try each available period (e.g. current year, previous year) until we get data.
        periods: list[dict[str, Any]] = [p for p in results if isinstance(p, dict)]

        all_readings: list[Reading] = []
        for period in periods:
            bis_raw = period.get("Bisdatum")
            if not isinstance(bis_raw, str):
                continue
            bis_dt = self._sap_date_to_datetime(bis_raw)
            if not bis_dt:
                continue

            bis_filter_dt = bis_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                hours=23
            )
            bis_filter = bis_filter_dt.strftime("%Y-%m-%dT%H:%M:%S")

            # Derive cost types present for this period from the Units expansion.
            unit_rows = (
                ((period.get("Units") or {}).get("results") or [])
                if isinstance(period, dict)
                else []
            )
            cost_types = [
                r.get("CostType")
                for r in unit_rows
                if isinstance(r, dict) and isinstance(r.get("CostType"), str)
            ]

            if kind == ReadingKind.heating:
                kotyps = [ct for ct in cost_types if ct.startswith("HZ")] or ["HZ01"]
                candidates = [(kotyps[0], True), (kotyps[0], False)]
            else:
                kotyps = [ct for ct in cost_types if ct.startswith("WW")] or ["WW01"]
                candidates = [(kotyps[0], True), (kotyps[0], False)]

            for kotyp, in_kwh in candidates:
                rel = (
                    f"CumuConsumptionMonSet?sap-client={quote(self.sap_client)}&$filter="
                    f"Nutzein%20eq%20%27{quote(self._user_unit_id)}%27%20and%20"
                    f"Bis%20eq%20datetime%27{quote(bis_filter)}%27%20and%20"
                    f"Kotyp%20eq%20%27{quote(kotyp)}%27%20and%20"
                    f"InKwh%20eq%20{'true' if in_kwh else 'false'}"
                )
                try:
                    data = await self._dashboard_batch_get(rel)
                except LoginError:
                    continue

                rows = (data.get("d") or {}).get("results") or []
                if not rows:
                    continue

                for row in rows:
                    dt = None
                    for k in ("Datum", "Bis"):
                        v = row.get(k)
                        if isinstance(v, str):
                            dt = self._sap_date_to_datetime(v)
                            if dt:
                                break
                    if not dt:
                        continue
                    val_raw = row.get("Verbrauch")
                    if val_raw is None:
                        continue
                    try:
                        val = float(str(val_raw).replace(",", "."))
                    except ValueError:
                        continue
                    unit = row.get("MassreadTxt") or row.get("Massread")
                    unit_s = str(unit) if unit else None
                    all_readings.append(Reading(timestamp=dt, value=val, unit=unit_s, kind=kind))

                if all_readings:
                    break

            if all_readings:
                break

        # De-duplicate by timestamp/value
        uniq: dict[tuple[datetime, float], Reading] = {}
        for r in all_readings:
            uniq[(r.timestamp, r.value)] = r
        return sorted(uniq.values(), key=lambda r: r.timestamp)

    async def get_consumption_comparison(
        self, *, period_index: int = 0
    ) -> dict[str, ConsumptionComparison]:
        """
        Return building/national consumption comparison (kWh/m²) for all HZ/WW cost types.

        Data from CumuConsLGCompSet:
        - your_value: Your consumption per m² (Verbrauch)
        - building_average: Building average (LgVerbrauch / Gebäudedurchschnitt)
        - national_average: National average (Refnutzer / Bundesdurchschnitt)

        Output keyed by cost_type. Use period_index to select period (0 = first).
        """
        if not self._logged_in:
            await self.login()
        if not self._user_unit_id:
            raise LoginError("Missing UserUnitID.")

        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        periods_list = [p for p in results if isinstance(p, dict)]
        if not periods_list or period_index < 0 or period_index >= len(periods_list):
            return {}
        period = periods_list[period_index]

        unit_rows = ((period.get("Units") or {}).get("results") or [])
        all_cost_types = sorted(
            {
                r["CostType"]
                for r in unit_rows
                if isinstance(r, dict) and isinstance(r.get("CostType"), str)
            }
        )
        wanted = [ct for ct in all_cost_types if ct.startswith(("HZ", "WW"))]

        bis_raw = period.get("Bisdatum")
        if not isinstance(bis_raw, str):
            return {}
        bis_dt = self._sap_date_to_datetime(bis_raw)
        if not bis_dt:
            return {}
        bis_filter_dt = bis_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            hours=23
        )
        bis_filter = bis_filter_dt.strftime("%Y-%m-%dT%H:%M:%S")

        out: dict[str, ConsumptionComparison] = {}
        for cost_type in wanted:
            rel = (
                f"CumuConsLGCompSet?sap-client={quote(self.sap_client)}&$filter="
                f"Nutzein%20eq%20%27{quote(self._user_unit_id)}%27%20and%20"
                f"Bis%20eq%20datetime%27{quote(bis_filter)}%27%20and%20"
                f"Kotyp%20eq%20%27{quote(cost_type)}%27%20and%20"
                f"InKwh%20eq%20true"
            )
            try:
                data = await self._dashboard_batch_get(rel)
            except LoginError:
                continue

            rows = (data.get("d") or {}).get("results") or []
            if not rows:
                continue

            row = rows[0]
            your_val = self._parse_sap_number(str(row.get("Verbrauch") or "0"))
            lg_val = self._parse_sap_number(str(row.get("LgVerbrauch") or "0"))
            ref_val = self._parse_sap_number(str(row.get("Refnutzer") or "0"))
            unit = row.get("MassreadTxt") or row.get("Massread")
            unit_s = str(unit) if unit else None
            kind = ReadingKind.heating if cost_type.startswith("HZ") else ReadingKind.hot_water

            out[cost_type] = ConsumptionComparison(
                cost_type=cost_type,
                your_value=your_val,
                building_average=lg_val,
                national_average=ref_val,
                unit=unit_s,
                kind=kind,
            )

        return out

    async def get_consumption_forecast(
        self, *, period_index: int = 0
    ) -> dict[str, ConsumptionForecast]:
        """
        Return forecast and year-over-year comparison for all HZ/WW cost types.

        Data from CumuConsMonCompSet (cumulative values, last month = full year):
        - current: Current year-to-date consumption (sum of monthly Verbrauch)
        - previous_year: Previous year total (last month's Vorjahr, cumulative)
        - forecast: Forecast for full year (last month's Prognose, cumulative)
        - difference: forecast - previous_year (Mehrverbrauch zum Vorjahr)

        Output keyed by cost_type. Use period_index to select period (0 = first).
        """
        if not self._logged_in:
            await self.login()
        if not self._user_unit_id:
            raise LoginError("Missing UserUnitID.")

        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        periods_list = [p for p in results if isinstance(p, dict)]
        if not periods_list or period_index < 0 or period_index >= len(periods_list):
            return {}
        period = periods_list[period_index]

        unit_rows = ((period.get("Units") or {}).get("results") or [])
        all_cost_types = sorted(
            {
                r["CostType"]
                for r in unit_rows
                if isinstance(r, dict) and isinstance(r.get("CostType"), str)
            }
        )
        wanted = [ct for ct in all_cost_types if ct.startswith(("HZ", "WW"))]

        bis_raw = period.get("Bisdatum")
        if not isinstance(bis_raw, str):
            return {}
        bis_dt = self._sap_date_to_datetime(bis_raw)
        if not bis_dt:
            return {}
        bis_filter_dt = bis_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            hours=23
        )
        bis_filter = bis_filter_dt.strftime("%Y-%m-%dT%H:%M:%S")

        out: dict[str, ConsumptionForecast] = {}
        for cost_type in wanted:
            rel = (
                f"CumuConsMonCompSet?sap-client={quote(self.sap_client)}&$filter="
                f"Nutzein%20eq%20%27{quote(self._user_unit_id)}%27%20and%20"
                f"Bis%20eq%20datetime%27{quote(bis_filter)}%27%20and%20"
                f"Kotyp%20eq%20%27{quote(cost_type)}%27%20and%20"
                f"InKwh%20eq%20true"
            )
            try:
                data = await self._dashboard_batch_get(rel)
            except LoginError:
                continue

            rows = (data.get("d") or {}).get("results") or []
            if not rows:
                continue

            # Values are cumulative per month; last row = full year totals
            # Sum Verbrauch for current YTD, take last row for Vorjahr/Prognose
            total_current = 0.0
            unit_s: str | None = None
            for row in rows:
                total_current += self._parse_sap_number(str(row.get("Verbrauch") or "0"))
                if not unit_s:
                    unit = row.get("MassreadTxt") or row.get("Massread")
                    unit_s = str(unit) if unit else None

            # Last row contains cumulative end-of-year values
            last_row = rows[-1]
            forecast_val = self._parse_sap_number(str(last_row.get("Prognose") or "0"))
            vorjahr_val = self._parse_sap_number(str(last_row.get("Vorjahr") or "0"))

            kind = ReadingKind.heating if cost_type.startswith("HZ") else ReadingKind.hot_water
            diff = round(forecast_val - vorjahr_val, 3)

            out[cost_type] = ConsumptionForecast(
                cost_type=cost_type,
                current=round(total_current, 3),
                previous_year=round(vorjahr_val, 3),
                forecast=round(forecast_val, 3),
                difference=diff,
                unit=unit_s,
                kind=kind,
            )

        return out

    async def get_room_consumption(
        self, *, period_index: int = 0
    ) -> dict[str, list[RoomConsumption]]:
        """
        Return room-level consumption breakdown (Raumvergleich) for all HZ/WW cost types.

        Data from CumuConsumptionRoomSet:
        - room_id: Room identifier (Raum)
        - room_name: Room description (RaumTxt)
        - share_percent: Share of total consumption (Anteil)
        - value: Consumption value for this room

        Output keyed by cost_type, each containing a list of rooms.
        Use period_index to select period (0 = first).
        """
        if not self._logged_in:
            await self.login()
        if not self._user_unit_id:
            raise LoginError("Missing UserUnitID.")

        dates = await self.get_dashboard_dates()
        results = (dates.get("d") or {}).get("results") or []
        periods_list = [p for p in results if isinstance(p, dict)]
        if not periods_list or period_index < 0 or period_index >= len(periods_list):
            return {}
        period = periods_list[period_index]

        unit_rows = ((period.get("Units") or {}).get("results") or [])
        all_cost_types = sorted(
            {
                r["CostType"]
                for r in unit_rows
                if isinstance(r, dict) and isinstance(r.get("CostType"), str)
            }
        )
        wanted = [ct for ct in all_cost_types if ct.startswith(("HZ", "WW"))]

        bis_raw = period.get("Bisdatum")
        if not isinstance(bis_raw, str):
            return {}
        bis_dt = self._sap_date_to_datetime(bis_raw)
        if not bis_dt:
            return {}
        bis_filter_dt = bis_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            hours=23
        )
        bis_filter = bis_filter_dt.strftime("%Y-%m-%dT%H:%M:%S")

        out: dict[str, list[RoomConsumption]] = {}
        for cost_type in wanted:
            rel = (
                f"CumuConsumptionRoomSet?sap-client={quote(self.sap_client)}&$filter="
                f"Nutzein%20eq%20%27{quote(self._user_unit_id)}%27%20and%20"
                f"Bis%20eq%20datetime%27{quote(bis_filter)}%27%20and%20"
                f"Kotyp%20eq%20%27{quote(cost_type)}%27"
            )
            try:
                data = await self._dashboard_batch_get(rel)
            except LoginError:
                continue

            rows = (data.get("d") or {}).get("results") or []
            if not rows:
                continue

            kind = ReadingKind.heating if cost_type.startswith("HZ") else ReadingKind.hot_water
            rooms: list[RoomConsumption] = []
            for row in rows:
                room_id = str(row.get("Raum") or "").strip()
                room_name = str(row.get("RaumTxt") or "").strip()
                share = self._parse_sap_number(str(row.get("Anteil") or "0"))
                value = self._parse_sap_number(str(row.get("Verbrauch") or "0"))
                unit = row.get("MassreadTxt") or row.get("Massread")
                unit_s = str(unit) if unit else None

                rooms.append(
                    RoomConsumption(
                        cost_type=cost_type,
                        room_id=room_id,
                        room_name=room_name,
                        share_percent=share,
                        value=value,
                        unit=unit_s,
                        kind=kind,
                    )
                )

            if rooms:
                out[cost_type] = rooms

        return out


async def _smoke_login(base_url: str, username: str, password: str) -> None:  # pragma: no cover
    async with BrunataClient(base_url=base_url, username=username, password=password) as c:
        await c.login()


def smoke_login(base_url: str, username: str, password: str) -> None:  # pragma: no cover
    asyncio.run(_smoke_login(base_url, username, password))

