"""Track sheets of the two historical recorders, transcribed from the box photographs.

Source: ``D:/Apollo_15_30-Track/orig_delivery/a15_images`` (the ``*_HEATMAP``/``*_heatmap``
photographs are the printed track sheets glued inside each box lid, dated 03-12-71).
Example sheets: ``A15_510-073_HR1U_heatmap.jpeg`` (HR1) and ``A15_510-074_HR2U_HEATMAP.jpg``
(HR2). The labels in the delivered filenames (``CH<n>_<label>``) follow these sheets exactly;
whether the delivered channel *numbers* do is what stage 10 tests.

``role`` is a short description of what a listener on that loop would hear. It is written
for the language model and deliberately says nothing about delivered channel numbers.
"""
from __future__ import annotations

# (true channel, short label as in delivered filenames, sheet text, CCS position, role)
HR1 = [
    (1, 'IRIG', 'GMT IRIG B FORMAT', '', 'Time code only: a buzzing IRIG-B carrier, no speech.'),
    (2, 'FD_OPS', 'FLIGHT OPS DIR', '3032', 'Flight Operations Director (senior management above the flight director); usually very quiet, occasional management calls.'),
    (3, 'MD', 'MISSION DIRECTOR', '3001', 'Mission Director (NASA HQ); quiet, management and press/status conversations.'),
    (4, 'DOD_REP', 'DOD REPRESENTATIVE', '3025', 'DOD representative in the MOCR; recovery forces, ships, aircraft, weather in recovery areas.'),
    (5, 'PROCEDURES', 'PROCEDURES', '3005', 'Procedures officer (OPS/ "Procedures"); network procedures, voice loop configuration, keying, comm procedures, coordination with remote sites.'),
    (6, 'ASST_FD', 'ASST FLIGHT DIRECTOR', '3004', 'Assistant Flight Director; coordination, logistics, schedules, MOCR housekeeping, often on the flight director loop.'),
    (7, 'FD_LT', 'FLIGHT DIRECTOR [L]', '3003', 'Flight Director, left jack: the flight director loop, "Flight" polling controllers (go/no-go), decisions.'),
    (8, 'FD_RT', 'FLIGHT DIRECTOR [R]', '3002', 'Flight Director, right jack: a second loop the flight director monitors/talks on.'),
    (9, 'FLIGHTPLAN', 'FLT PLANS OFFICER', '3028', 'Flight Activities / flight plan officer (FAO): crew timeline, flight plan updates, checklist pages, crew activities, staff support room.'),
    (10, 'NTWK_LT', 'NETWORK CONTROLLER [L]', '3030', 'Network controller, left: ground station network (MSFN), sites (Goldstone, Madrid, Honeysuckle, Hawaii, ...), handovers, configurations, data lines.'),
    (11, 'NTWK_RT', 'NETWORK CONTROLLER [R]', '3031', 'Network controller, right: second network loop, Goddard/NASCOM, site status.'),
    (12, 'COMM_SUP_1', 'COMM SUPPORT 1', '3064', 'Communications support 1: comm technicians, circuit checks, voice loop and line problems.'),
    (13, 'SURGEON_RT', 'SURGEON [R]', '3008', 'Flight surgeon: crew biomedical data, heart rates, sleep, medical conferences.'),
    (14, 'CAPCOM_LT', 'CAPCOM [L]', '3010', 'CAPCOM, left jack: the spacecraft communicator, talks to the crew (air-to-ground) and is called "CAPCOM" on the flight loop.'),
    (15, 'CAPCOM_RT', 'CAPCOM [R]', '3009', 'CAPCOM, right jack: second loop at the CAPCOM console (often the back room / support loop).'),
    (16, 'ALSEP_NTWK', 'ALSEP NETWORK CONTROL', '4410', 'ALSEP network control: lunar surface experiments package, ALSEP commands and data, sites tracking ALSEP.'),
    (17, 'POS_EECOM', 'POS EECOM ENGINEER', '3012', 'EECOM (CSM electrical, environmental, consumables): cryo, fuel cells, power, oxygen, CSM systems.'),
    (18, 'CSM_GNC', 'CSM GNC ENGINEER', '3013', 'CSM GNC engineer: CSM guidance/navigation/control hardware, SPS engine, RCS, attitude.'),
    (19, 'RETRO_OF', 'RETROFIRE OFFICER', '3022', 'Retrofire officer (RETRO): entry, abort and return-to-Earth planning, PADs for entry.'),
    (20, 'FLT_DO', 'FLT DYNAMICS OFFICER', '3019', 'Flight dynamics officer (FIDO): trajectory, burns, vectors, ephemeris, maneuvers.'),
    (21, 'GUIDO_LT', 'GUIDANCE OFFICER [L]', '3020', 'Guidance officer, left: onboard computer, state vector and REFSMMAT uplinks, P-programs.'),
    (22, 'GUIDO_RT', 'GUIDANCE OFFICER [R]', '3021', 'Guidance officer, right: second guidance loop (guidance support room).'),
    (23, 'CMD_LC', 'CMD LOAD CONTROL', '3505', 'Command load control: building and sending command loads to sites, load numbers, uplink verification.'),
    (24, 'RTC_CNTRL', 'REAL TIME CMD CONTROL', '3506', 'Real-time command control: real-time commands to the spacecraft through remote sites.'),
    (25, 'CCATS_CC', 'CCATS CMD CONTROL', '3507', 'CCATS command control: the communications, command and telemetry system computers; command and telemetry routing.'),
    (26, 'TLM_IC_LT', 'TLM INSTR CONTROL [L]', '3508', 'Telemetry instrumentation control, left: telemetry formats, PCM, data quality, site telemetry configuration.'),
    (27, 'TLM_IC_RT', 'TLM INSTR CONTROL [R]', '3509', 'Telemetry instrumentation control, right.'),
    (28, 'TRACK_LT', 'INSTR TRACK CNTRL [L]', '3510', 'Instrumentation / tracking control, left: radar tracking data, site tracking, high-speed data.'),
    (29, 'TRACK_RT', 'INSTR TRACK CNTRL [R]', '3511', 'Instrumentation / tracking control, right.'),
    (30, 'VOICE_TECH', 'VOICE ANNO/TECH OPS 1', '9151', 'Voice annotation / tech ops: a technician periodically reads the recorder track sheets aloud ("Channel 2, Flight Operations Director, position 3032 ..."), plus technical operations chatter.'),
]

HR2 = [
    (1, 'IRIG', 'GMT IRIG B FORMAT', '', 'Time code only: a buzzing IRIG-B carrier, no speech.'),
    (2, 'NR_CRD', 'NASA RECOVERY COORD', '4280', 'NASA recovery coordinator: recovery forces, carrier/ship, helicopters, swimmers, weather in recovery zones; quiet except launch/entry.'),
    (3, 'ASST_NR_CRD', 'ASST NASA RCVY COORD', '4281', 'Assistant NASA recovery coordinator; recovery logistics.'),
    (4, 'RD_CONT_1', 'RECOVERY DISPLAY CONT 1', '4282', 'Recovery display control: recovery displays, plotting of recovery forces.'),
    (5, 'COMM_SUP_2', 'COMM SUPPORT 2', '3065', 'Communications support 2: comm technicians, circuit and loop problems.'),
    (6, 'DOD_CRD', 'DOD COORD', '4288', 'DOD coordinator: recovery forces, DOD ships and aircraft, ARIA aircraft.'),
    (7, 'DOD_PRIM_OP', 'DOD PRIMARY OP', '4289', 'DOD primary operations.'),
    (8, 'DOD_M', 'DOD MANAGER', '4284', 'DOD manager.'),
    (9, 'DOD_EXEC_OF', 'DOD EXECUTIVE OFFICER', '4285', 'DOD executive officer.'),
    (10, 'DOD_AC_1', 'DOD ASST FOR COMM 1', '4291', 'DOD assistant for communications: DOD comm links, ships/aircraft comm.'),
    (11, 'DOD_PIO', 'DOD PIO', '4290', 'DOD public information officer.'),
    (12, 'COMTECH', 'COMM TECH', '4499', 'Comm tech: voice/comm technicians, patching, loop checks ("Houston comm tech").'),
    (13, 'COMCNTRL', 'COMM CONTROLLER', '4497', 'Comm controller: communications control, circuit assignments, NASCOM lines.'),
    (14, 'SPACE_ENV', 'SPACE ENVIRONMENT', '4266', 'Space environment: solar flares, radiation, solar particle events, dosimetry.'),
    (15, 'COMP_SUP', 'COMPUTER SUPPORT', '3402', 'Computer support: RTCC / mission operations computers, displays, processing.'),
    (16, 'POS_SSR_1R', 'POS SSR 1R', '3151', 'A staff support room position (SSR 1, right).'),
    (17, 'BST_LT', 'BOOSTER [L]', '3016', 'Booster (Saturn launch vehicle) left: S-IVB, launch vehicle systems; busy around launch, TLI, S-IVB impact.'),
    (18, 'BST_SYS_ENG', 'BOOSTER SYS ENG', '3018', 'Booster systems engineer.'),
    (19, 'BST_RT', 'BOOSTER [R]', '3017', 'Booster right.'),
    (20, '3_FD_L', '3*FLT DIR LOOP', 'I-301', 'The flight director loop itself (conference loop): "Flight", controllers reporting.'),
    (21, '3_AFD_C_L', '3*AFD CONF LOOP', 'L-091', 'Assistant flight director conference loop.'),
    (22, '3_GOSS_2_L', '3*GOSS 2 LOOP', 'I-277', 'GOSS 2 loop (ground operational support system network loop): network/site coordination.'),
    (23, 'INCO', 'INCO', '3006', 'INCO (instrumentation and communications officer): spacecraft comm, antennas, TV, S-band, data storage equipment.'),
    (24, '3_MOCR_D_L', '3* MOCR DYN LOOP', 'L-079', 'MOCR dynamics loop: trajectory team (FIDO, RETRO, GUIDO) conference loop.'),
    (25, '3_GOSS_C_L', '3*GOSS CONF LOOP', 'I-275', 'GOSS conference loop (network conference).'),
    (26, '3_GOSS_4_L', '3*GOSS 4 LOOP', 'I-300', 'GOSS 4 loop (network loop).'),
    (27, 'LM_GNC', 'LM GNC ENGINEER', '3027', 'LM GNC (Control): LM guidance hardware, descent/ascent engines, LM RCS.'),
    (28, 'LM_EECOM', 'LM EECOM ENGINEER', '3014', 'LM EECOM / TELMU: LM electrical and environmental systems, batteries, suits/PLSS.'),
    (29, 'EXPMT_AO', 'EXPMT ACTIVITIES OFFICER', '3029', 'Experiments activities officer: science experiments, SIM bay, cameras, lunar surface experiments.'),
    (30, 'VOICE', 'VOICE ANNOTATION', '', 'Voice annotation: a technician periodically reads the recorder track sheets aloud ("Channel 2, NASA recovery coordinator, position 4280 ...").'),
]

SHEETS = {'HR1': HR1, 'HR2': HR2}

# True CH01 carries IRIG-B time code. Where stage 10 decodes time code on a delivered channel
# (channelid_irig_nara.py), that channel is true CH01; where a full-length decode of the
# candidate channels finds none, true CH01 is not on the delivery.
IRIG_CHANNEL = 1


def recorder_of(head: str) -> str:
    """'HR1U' -> 'HR1'."""
    return head[:3]


def sheet(recorder: str) -> dict[int, dict]:
    return {ch: dict(channel=ch, label=label, sheet=text, position=pos, role=role)
            for ch, label, text, pos, role in SHEETS[recorder]}
