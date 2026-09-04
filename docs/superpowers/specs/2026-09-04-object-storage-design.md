# Object storage: file uploads for survey responses

Status: approved 2026-09-04. Slice 1 of a larger plan.

## Context

The system had no file handling: exports stream from memory, the client has no
file input, and `QuestionType` covers only text / rating / multiple-choice /
boolean. A Tigris S3-compatible bucket (`CSS_OSS`) is attached to the
`customer-survey-server` Railway service, which injects `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `AWS_ENDPOINT_URL` and
`AWS_S3_BUCKET_NAME`.

Verified against the live bucket before designing:

- credentials work (`head_object` / `put` / `get` / `delete` round-trip)
- `generate_presigned_post` with a `content-length-range` condition **is
  enforced**: an in-range body returned 200, an over-range body returned 400
- `GetBucketCors` is supported
- `GetBucketPolicy` is **NotImplemented** — there is no public-read path, so
  every download must be a presigned GET

## Scope of this slice

Storage core + the `file` question type end-to-end. Survey branding assets,
persisted exports and a generic attachments library reuse the same core and
are deliberately out of scope until separately approved.

## Design

### Storage core — `app/storage.py`

Wraps boto3. Three operations: `presign_upload` (presigned POST carrying a
`content-length-range` policy), `presign_download` (presigned GET with a
content-disposition filename), `delete`.

If the `AWS_*` variables are absent the module reports itself disabled and the
endpoints return 503. This keeps the SQLite test suite and local dev working
without credentials, mirroring how `resend` is already stubbed in
`app/tests/conftest.py`.

### Data model — one polymorphic `attachments` table

    id, key, filename, content_type, size_bytes,
    owner_type (response | survey_asset | export | library),
    owner_id, uploaded_by, status (pending | committed), created_at

Uploads are two-phase. Presign writes a `pending` row; `confirm` calls
`head_object` and promotes to `committed` only if the object really exists at
the claimed size. The client is never trusted about what landed.

**Ownership wrinkle.** A response attachment is uploaded before its `Response`
row exists. For `owner_type=response`, `owner_id` therefore holds the
`SurveyDistribution.id` (the respondent's token) during upload, and
`submit_response` re-parents those rows to the new `Response.id` on submit.
This also makes the per-token cap a simple count.

### Endpoints — `app/routers/files.py`, prefix `/api/files`

- `POST /upload-url` — mint a presigned POST + `pending` row
- `POST /{id}/confirm` — verify via `head_object`, promote to `committed`
- `GET /{id}/download` — presigned GET
- `DELETE /{id}` — remove object and row
- `GET /?ownerType=&ownerId=` — list

### Authorization

Respondent uploads are necessarily unauthenticated, so they are gated on a
valid, unused `SurveyDistribution` token for a **published, in-window** survey
— the same guards `submit_response` enforces — plus:

- max 5 attachments per token
- 5-minute presign TTL
- allowlist: JPEG, PNG, WebP, GIF, PDF
- 10 MB per file, enforced by the bucket policy

Everything else requires an authenticated manager or admin. Reading a response
attachment is staff-only.

Because bucket policies are unavailable, a `survey_asset` download is the one
unauthenticated read path — relevant to a later slice, noted here so the
asymmetry is not mistaken for an oversight.

### Housekeeping

Uncommitted uploads still occupy the bucket. A janitor deletes `pending` rows
older than one hour, and their objects, on startup and opportunistically.

### Client changes (`customer_survey_client`)

- `FileUpload` component: request URL → POST direct to Tigris → confirm
- `file` question type rendered in `SurveyForm`, offered in the survey editor
- attachment links in `DetailedAnalytics`
- `export.py` needs a renderer for file answers or the CSV prints a dict repr

### Testing

`app/storage.py` is stubbed in `conftest.py` as `resend` already is, so no test
touches the network. Coverage: presign gating (unpublished, closed, bad token,
over-cap), confirm rejecting a missing object, staff-only reads, re-parenting
on submit.

## Consequences

- Bucket CORS must allow the client origin and `localhost:3000` (approved).
- `boto3` joins `requirements.txt`.
- The seeded `admin@css.com / Password123!` account gains upload and download
  rights over the whole bucket. It should be changed.
