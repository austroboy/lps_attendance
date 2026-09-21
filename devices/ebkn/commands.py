"""
Commands we can queue for a terminal.

The device asks "anything for me?" every few seconds (request_code=receive_cmd).
If a command is waiting we answer with cmd_code + trans_id headers and the
parameter JSON in the body. The device runs it and reports back with
request_code=send_cmd_result.
"""

GET_DEVICE_STATUS = "GET_DEVICE_STATUS"
SET_TIME = "SET_TIME"
GET_USER_ID_LIST = "GET_USER_ID_LIST"
GET_LOG_DATA = "GET_LOG_DATA"
DELETE_USER = "DELETE_USER"
SET_USER_NAME = "SET_USER_NAME"
SET_USER_PRIVILEGE = "SET_USER_PRIVILEGE"
GET_USER_INFO = "GET_USER_INFO"
SET_USER_INFO = "SET_USER_INFO"
SET_ENROLL_DATA = "SET_ENROLL_DATA"
GET_ENROLL_DATA = "GET_ENROLL_DATA"
CLEAR_LOG_DATA = "CLEAR_LOG_DATA"
CLEAR_ENROLL_DATA = "CLEAR_ENROLL_DATA"
SET_FK_NAME = "SET_FK_NAME"

# label, parameter hint, whether it is destructive
CATALOGUE = [
    (GET_DEVICE_STATUS, "Read device status", "{}", False),
    (SET_TIME, "Sync the clock", '{"time": "20260914103000"}', False),
    (GET_USER_ID_LIST, "List enrolled user ids", "{}", False),
    (GET_LOG_DATA, "Pull stored punch log",
     '{"begin_time": "20260901000000", "end_time": "20260930235959"}', False),
    (GET_USER_INFO, "Read one user", '{"user_id": "1"}', False),
    (SET_USER_NAME, "Rename a user", '{"user_id": "1", "user_name": "Rahim"}', False),
    (SET_USER_PRIVILEGE, "Change privilege",
     '{"user_id": "1", "user_privilege": "USER"}', False),
    (DELETE_USER, "Delete a user", '{"user_id": "1"}', True),
    (SET_FK_NAME, "Rename the terminal", '{"fk_name": "GATE-1"}', False),
    (CLEAR_LOG_DATA, "Erase punch log on the device", "{}", True),
    (CLEAR_ENROLL_DATA, "Erase all enrolments", "{}", True),
]

CHOICES = [(code, f"{code} — {label}") for code, label, _hint, _d in CATALOGUE]
HINTS = {code: hint for code, _l, hint, _d in CATALOGUE}
DESTRUCTIVE = {code for code, _l, _h, destructive in CATALOGUE if destructive}
