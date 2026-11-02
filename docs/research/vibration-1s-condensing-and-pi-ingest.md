# 50 Hz vibration: 1 s condensing statistics and PI ingest options

Date: 2026-11-02. Scope: 4 loggers on a wind turbine, 50 Hz CSV, backlog plus periodic push to PI, drift and health-change detection.

## Source reliability legend

- **[P]** primary: standards-body or AVEVA/OSIsoft official page (as surfaced by search result summaries).
- **[S]** secondary: community forum, vendor blog, feedback portal. Treat as unverified.
- **[E]** engineering judgement by the author of this note, no citation. Not a sourced claim.

Important limitations of this research:

- Most `docs.aveva.com` pages are rendered by JavaScript. Direct fetches returned empty shells, so AVEVA statements below come from search-result excerpts of those pages, not from a full read. Re-check parameter names on the live pages before coding.
- ISO, IEC and VDI standards are paywalled. Only abstracts, scope pages and catalogue listings were consulted. No normative clause text (limits, filter bands, averaging times) was verified.
- The cmdlet `Update-PIValue` named in the brief was not found in the PowerShell Tools documentation. The documented value-writing cmdlet found is `Add-PIValue` (see section 2.4).

---

## 1. Per-second statistics when reducing 50 Hz data

### 1.1 What the standards actually cover (abstract level only)

| Standard | What the abstract/scope says | Relevance here | Source |
|---|---|---|---|
| ISO 20816-1:2016 | General conditions and procedures for measuring and evaluating vibration on rotating, non-rotating and non-reciprocating parts of complete machines. Defines displacement, velocity, acceleration as basic parameters. Replaced ISO 10816-1. Torsional vibration excluded. A 2nd edition (ISO/FDIS 20816-1) was in approval. | Defines the RMS-velocity style of evaluation. Its velocity band (commonly cited as 10-1000 Hz) could **not** be verified from the sources found. | [P] https://www.dinmedia.de/en/standard/din-iso-20816-1/267858541 ; https://www.iso.org/standard/89921.html |
| ISO 10816-21:2015 (wind turbines with gearbox >200 kW, measured on non-rotating parts) | Measurement and evaluation of vibration on wind turbines and components using sensors on stationary parts. Gives evaluation zones for continuous load operation but no normative zone-boundary values (annex gives informative onshore values from about 1000 turbines up to 3 MW). Diagnostics/fault detection out of scope; refers to ISO 13373-2 for bearing/gearbox defect detection. Revised by ISO/DIS then FDIS 20816-21 (broader scope incl. gearless). Publication status of the new edition not confirmed. | Confirms that wind-turbine evaluation is about steady-state, load-binned vibration on stationary parts, not low-frequency tower motion. | [P/catalogue] https://knowledge.bsigroup.com/products/mechanical-vibration-evaluation-of-machine-vibration-by-measurements-on-non-rotating-parts-horizontal-axis-wind-turbines-with-gearbox ; https://genorma.com/en/standards/iso-dis-20816-21 |
| VDI 3834 Part 1 (2015-08, wind turbines with gearbox; 2009 edition withdrawn). Part 2 (2022-06) for gearless. | Describes effects on tower/nacelle vibration from wind, waves (offshore), flow disturbance in front of the tower, natural vibration of blades and structure. Assessment values from a statistical analysis of many turbines. Does not cover torsional vibration or bearing/gear condition. | Closest match to the use case (tower/nacelle structural motion), but guide values are in the paywalled text and not verified. | [P/catalogue] https://vdi.de/en/home/vdi-standards/details/vdi-3834-blatt-1-measurement-and-evaluation-of-the-mechanical-vibration-of-wind-turbines-and-their-components-wind-turbines-with-gearbox ; https://www.dinmedia.de/de/technische-regel/vdi-3834-blatt-1/234798114 |
| ISO 13373-1:2002 | General guidelines for measurement and data collection for vibration condition monitoring of rotating machines: methods, parameters, transducer selection/location/attachment, operating conditions, signal conditioning, interfaces to data processing. Last confirmed 2024. | Source for the general idea of trended measurement parameters and recording operating condition with the data. | [P] https://www.iso.org/standard/21831.html |
| IEC 61400-25 series | Communication between wind plant components (turbines) and supervisory systems such as SCADA: information models (25-2), information exchange (25-3), mapping to protocols (25-4), conformance (25-5). Not about signal processing. | Only relevant if tags must be named/mapped to an IEC 61400-25 information model. Gives no statistic definitions. | [P/catalogue] https://standards.globalspec.com/std/9924046/iec-61400-25-2 ; https://vde-verlag.de/iec-normen/preview-pdf/info_iec61400-25-1{ed1.0}en.pdf |

Takeaway: none of the standards above prescribe 1 s statistics of a 50 Hz stream. They define band-limited RMS (velocity) evaluation on stationary parts and trending practice. Everything in 1.2 to 1.5 is therefore [E] unless noted. The ISO 20816 / 10816-21 velocity evaluation uses a band that normally starts at about 10 Hz and goes into the kHz range [E, band unverified]. A 50 Hz stream (Nyquist 25 Hz) cannot be compared against those zone boundaries. At best it supports relative trending.

### 1.2 Nyquist limits [E, standard signal-processing facts]

- Usable bandwidth is below 25 Hz and in practice below about 20 Hz, unless the loggers apply an anti-alias filter before sampling. Whether they do is unknown (open question). Without one, energy above 25 Hz folds into the band and RMS/kurtosis can be contaminated.
- Content at that rate: tower and nacelle first modes, rotor 1P/3P harmonics, blade-pass related motion, wind/wave excitation. Not bearing/gear mesh/kHz.
- Units matter: if channels are acceleration, integrating to velocity at low frequency amplifies noise and DC; high-pass filter first. If channels are already velocity, 1 s RMS is meaningful as a relative index but not as an ISO 20816 value.
- A 1 s window at 50 Hz is 50 samples. FFT resolution is 1 Hz, so the tower first mode (typically a fraction of a Hz to about 1 Hz for large turbines [E]) cannot be resolved in 1 s windows. Use a longer window (e.g. 10 min) for spectral features.

### 1.3 Candidate per-second statistics [E]

| Statistic | Useful for | Caveat at 50 samples/s |
|---|---|---|
| Mean (DC offset) | Sensor drift, tilt/inclination change (static gravity component on accelerometers), thermal drift, loose mounting | Keep it: best drift indicator. RMS must be computed AC-coupled (about the mean) so the two stay separate. |
| RMS (AC-coupled) / std | Overall vibration level, the main trend variable | For AC-coupled data RMS equals std (population). Store one. |
| Min, Max (peak, peak-to-peak) | Shock, impacts, clipping, range violations | Cheap. Peak-to-peak = max - min. |
| Crest factor (peak/RMS) | Impulsiveness | Low value here: bearing-fault impulsiveness lives in kHz, invisible at 25 Hz. Derive downstream from stored peak and RMS. |
| Kurtosis | Impulsive/non-Gaussian content | Noisy with 50 samples. Compute over 1 to 10 min windows if at all. |
| Band-limited RMS (e.g. 0.1-1 Hz, 1-5 Hz, 5-25 Hz) | Isolates tower mode, rotor harmonics, higher structure; most useful for health/change detection | A 1 s window cannot cleanly filter below about 1 Hz. Filter the continuous stream first (IIR/FIR with state carried across files), then take 1 s RMS per band. |
| Dominant frequency via FFT | Tracking natural-frequency shift (stiffness, foundation, scour, ice) | Needs minutes-long windows for sub-Hz resolution. Publish at 1 to 10 min cadence, not 1 s. |

### 1.4 Recommended minimal set (suggested tags per channel) [E]

Per channel, 1 s cadence:

1. `<ch>.Mean` (DC offset; drift)
2. `<ch>.RMS` (AC-coupled overall level)
3. `<ch>.Min` and `<ch>.Max`
4. `<ch>.BandRMS_<band>` for 2 to 3 bands chosen from the turbine's tower frequency and rotor-speed range

Slower cadence (10 min):

5. `<ch>.DomFreq_tower` and `<ch>.PeakAmp_tower` from a long-window FFT/Welch estimate.
6. Optional: kurtosis on the 10 min window.

Data-quality tags, per logger and/or channel at 1 s:

- `SampleCount` (expected 50): under/over-count shows dropped or duplicated samples.
- `GapFlag` / `GapSeconds`: missing seconds, file-boundary gaps.
- `Flatline`: raw-sample std equals 0 over N seconds (stuck sensor, disconnection).
- `Clipped`: fraction of samples at ADC rails or sensor full scale.
- Optional: `ClockSkew` (logger time vs server time), `FileLatency` (arrival minus data time).

Also write turbine operating state (rotor speed, power, wind speed) as PI tags from SCADA. ISO 13373-1's abstract lists operating conditions as part of measurement practice (source in 1.1), and drift/change detection should compare like-for-like operating bins [E].

### 1.5 Drift and major-change detection [E]

- Drift: trend `Mean` and band-RMS per operating bin with a slow EWMA or CUSUM against a baseline period.
- Major change: shift of `DomFreq_tower`, step in band-RMS at the same operating bin, new flatline or clipping.
- Detection can run in PI (Asset Analytics) or in the ingest program. Keep raw 50 Hz CSV as system of record since PI only holds 1 s values.

---

## 2. PI ingest options

### 2.0 Facts about the PI Data Archive that shape any option

- **Out-of-order (OOO) events skip compression.** Events with a timestamp older than the current snapshot go straight to the event queue without compression testing. [P] https://docs.aveva.com/bundle/pi-server-s-da-admin/page/1021696.html (via search excerpt)
- **Backfill guidance:** compression is bypassed for data written before the snapshot time. For compressed backfill, send data time-sequentially, oldest first. If backfilled values predate the oldest archive, create a new dynamic archive starting at or before the oldest timestamp and ending at the start of the current oldest archive. Tuning parameter `Snapshot_DoNotReplacePTCreatedOnOOO` affects compression of backfill. [P] https://docs.aveva.com/bundle/pi-server-s-da-admin/page/1022260.html ; https://docs.aveva.com/bundle/pi-server-s-da-admin/page/1021213.html (via search excerpts; per-page content not individually confirmed)
- Importing into points that already hold values means the data cannot be compressed. [P, same pages]
- **PI Buffer Subsystem (pibufss)** stores and forwards events to the Data Archive, resending in the order received after reconnection, and exposes an "Out-Of-Order Events/sec" counter. AVEVA recommends pibufss over bufserv (older, PI API based) for most scenarios. [P] https://docs.aveva.com/bundle/glossary/page/PI_Buffer_Subsystem.html.html ; https://docs.aveva.com/bundle/pi-interface-configuration-utility-icu/page/1011126.html ; https://docs.aveva.com/bundle/pi-server-s-buf-ha/page/1020068.html
- Reports that OOO writes failed through the buffer when it was not yet initialised, and that analysis backfill via the buffer arrived uncompressed. [S] https://pisystem.feedback.aveva.com/ideas/PIINTF-I-251 ; https://feedback.aveva.com/ideas/PIDA-I-1312
- **Duplicates / timestamp precision:** the archive is designed for one value per timestamp. Default write (Replace) overwrites; Insert allows multiples. PI timestamp precision is reported as 1/65536 s. [S] https://community.aveva.com/pi-square-community/f/forum/86593/multiple-afvalues-at-the-same-timestamp-using-af-sdk . At a 1 s cadence collisions only arise from bugs or reprocessing.
- Future-dated data: not researched; likely needs future-enabled tags [E, unverified]. Not relevant if only past data is pushed.

### 2.1 Update options

From the PI Web API Update Option topic [P] https://docs.aveva.com/bundle/pi-web-api-reference/page/help/topics/update-option.html (search excerpt):

| Option | Behaviour |
|---|---|
| Replace (default) | Add the value; if values exist at that time, one is overwritten. |
| Insert | Add the value and keep existing values at that time. |
| NoReplace | Add only if no value exists at that time. |
| ReplaceOnly | Overwrite an existing value; ignore the input if none exists. |
| InsertNoCompression | Insert the value without compression. |
| Remove | Delete the value at that time. |

The AF SDK `AFUpdateOption` enum page could not be fetched. An AF SDK sample comment says Insert and InsertNoCompression are interchangeable in that call [P/excerpt] https://docs.aveva.com/bundle/af-sdk/page/html/M_OSIsoft_AF_Asset_AFDataReference_UpdateValues.htm . Verify the member list on the live enum page https://docs.aveva.com/bundle/af-sdk/page/html/T_OSIsoft_AF_Data_AFUpdateOption.htm .

### 2.2 PI Web API

- Endpoints: `POST streams/{webId}/recorded` and streamset `updatevalues`. `updateOption` default Replace, `bufferOption` default BufferIfPossible. Status codes: 202 accepted for potential buffered update, 204 all succeeded, 207 partial success (per-item substatus in input order), 409 not supported by data reference. [P] https://docs.aveva.com/bundle/pi-web-api-reference/page/help/controllers/stream/actions/updatevalues.html ; https://docs.aveva.com/bundle/pi-web-api-reference/page/help/controllers/streamset/actions/updatevalues.html
- Batch controller: dictionary of sub-requests. [P] https://docs.aveva.com/bundle/pi-web-api/page/1023110.html ; run sequentially on the host per [S] https://community.aveva.com/pi-square-community/f/forum/95537/pi-webapi-limit-for-batch-request
- `MaxReturnedItemsPerCall` caps items returned per call (default 150,000 on one AVEVA page; a topology guide shows 2,000,000 as a sample). Whether it applies to write payloads was **not found**. [P] https://docs.aveva.com/bundle/pi-web-api/page/1023030.html ; https://docs.aveva.com/bundle/pi-server-l-topologies/page/1396883.html
- Not verified: request body size limits; accepted `bufferOption` values (only a third-party page lists them [S] https://www.nuget.org/packages/piwebapi).
- Pros [E]: HTTPS, language-agnostic, no client install. Cons: extra hop and component to secure, chunking needed for backlog, slower than AF SDK for very large loads.

### 2.3 AF SDK (.NET)

- `PIPoint.UpdateValues(IList<AFValue>, AFUpdateOption)` with an overload adding `AFBufferOption`. Update option controls duplicate handling and can remove values. If not set in AFSDK.config, buffer option defaults to BufferIfPossible. Buffered writes need PI Buffer Subsystem pre-configured, and error feedback from the Data Archive cannot be returned to the caller when buffering through pibufss. [P/excerpt] https://docs.aveva.com/bundle/af-sdk/page/html/M_OSIsoft_AF_PI_PIPoint_UpdateValues_1.htm ; https://docs.aveva.com/bundle/af-sdk/page/html/M_OSIsoft_AF_PI_PIPoint_UpdateValue_1.htm
- Bulk and async variants: `PIServer.UpdateValues` (many points per call, overloads with AFUpdateOption/AFBufferOption), `UpdateValuesAsync`. [P] https://docs.aveva.com/bundle/af-sdk/page/html/Overload_OSIsoft_AF_PI_PIServer_UpdateValues.htm ; https://docs.aveva.com/bundle/af-sdk/page/html/M_OSIsoft_AF_PI_PIPoint_UpdateValuesAsync.htm
- Pros [E]: fastest and richest for bulk, per-value errors when unbuffered, native AFValue with status flags. Cons: needs AF SDK installed on the runner.

### 2.4 PowerShell Tools for the PI System

- `Update-PIValue` was **not found**. Search returned `Add-PIValue` (adds values to a PI Data Archive, takes PIPoint objects from `Get-PIPoint`), `Remove-PIValue`, `Set-PIPoint`. [P/excerpt] https://docs.aveva.com/bundle/pi-powershell-2.2.3/page/html/T_OSIsoft_PowerShell_AddPIValue.htm ; https://docs.aveva.com/bundle/pi-powershell-3.1.1/page/html/P_OSIsoft_PowerShell_AddPIValue_Value.htm ; https://docs.aveva.com/bundle/pi-powershell-3.1.1/page/html/P_OSIsoft_PowerShell_RemovePIValue_PIPoint.htm
- Whether `Add-PIValue` exposes update/buffer options could **not** be verified. Run `Get-Help Add-PIValue -Full` and `Get-Command *PIValue*` on the target machine.
- Pros [E]: simple, scheduled-task friendly. Cons: slow per-value pipeline for millions of events, and poor fit for the DSP. Good for tag provisioning and spot checks.

### 2.5 Comparison

| Criterion | PI Web API | AF SDK | PowerShell Tools |
|---|---|---|---|
| Bulk backlog | OK in chunks, slower [E] | Best (bulk UpdateValues, async) [E] | Poor at volume [E] |
| Out-of-order / backdated | Supported; compression bypassed for OOO (archive docs) | Same | Same via AF SDK [E] |
| Duplicates | updateOption, default Replace | AFUpdateOption | Unverified |
| Buffering | bufferOption, 202 when buffered | AFBufferOption; no per-event error feedback when buffered | Unverified |
| Error feedback | 207 per-item | Per-value errors if unbuffered [E] | Cmdlet errors |
| Deploy | PI Web API server | AF SDK on host | Module plus AF SDK |

### 2.6 Design points for backlog and periodic runs [E unless cited]

- **Compression/exception:** backdated loads are stored uncompressed (2.0) while live data is compressed if the tag compresses. Set compression explicitly per tag (for 1 s statistics, probably off) to keep history consistent. Volume: 86,400 events/day/tag; 4 loggers x ~10 tags is about 3.5 M events/day.
- **Archive coverage [P, 2.0]:** ensure an archive covers the backlog start; otherwise create a dynamic archive for the missing range first. The brief's "primary archive" wording was not found in the docs; "dynamic archive" and "oldest archive" were.
- **Buffering vs direct:** backlog direct (unbuffered) so errors return and rate is controlled. Periodic runs buffered only if pibufss is configured and silent error loss is acceptable.
- **Time:** convert to UTC once at parse, build AFTime from UTC, never from `LocalTime`; handle DST if loggers write local time. Timestamp = start of the second, documented.
- **Idempotency:** output is deterministic (tag, UTC second, value), so re-sending with Replace is safe; avoid Insert on reruns (duplicates).
- **Checkpoints:** per file (path, size, mtime, hash) and per channel last complete second, in a small atomically written state file. Skip files still being appended (stable size/mtime), carry the partial second and filter state across files.
- **Chunking:** write oldest-first per tag in chunks (e.g. a day or ~100k events; tune empirically). Read back a sample window to verify.

---

## Recommendations

1. Per channel compute 1 s `Mean`, AC `RMS`, `Min`, `Max`, 2 to 3 band-limited RMS, plus 10 min dominant tower frequency/amplitude. Keep raw CSV. Skip per-second crest factor and kurtosis. Do not claim ISO 20816 / 10816-21 compliance (25 Hz Nyquist, unknown sensor units).
2. Add data-quality tags: SampleCount, gap, flatline, clipping, file latency.
3. Use AF SDK (`PIServer.UpdateValues` / `PIPoint.UpdateValues`) from a .NET app, or PowerShell loading the AF SDK assembly, with `AFUpdateOption.Replace`, direct writes for the backlog, buffered or direct for periodic runs. PowerShell Tools for provisioning and checks; PI Web API only if AF SDK cannot be installed.
4. Set tag compression explicitly, ensure archive coverage, load oldest first, checkpoint per file/channel, UTC everywhere.

## Open questions

- Sensor type and units (acceleration/velocity/displacement)? Anti-alias filter in the loggers?
- CSV timestamps: UTC or local, time sync source, clock skew?
- Tower first natural frequency and rotor speed range (to choose bands)?
- Is PI Buffer Subsystem configured on the runner host? PI Data Archive version, backlog range vs oldest archive?
- Tag compression and retention policy; who creates the tags (program vs admin), naming, AF template?
- Does `Add-PIValue` in the installed module expose update/buffer options (verify with `Get-Help`)?
- Any write payload size limit on the PI Web API in this deployment?
- Is formal ISO 10816-21 / VDI 3834 compliance needed (would require buying the standards and higher-bandwidth sensors)?
