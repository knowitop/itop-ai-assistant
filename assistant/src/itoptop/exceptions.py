itop_error_codes = {
    0: "OK - No issue has been encountered",
    1: "UNAUTHORIZED - Missing/wrong credentials or the user does not have enough rights to perform the requested operation",
    2: "MISSING_VERSION - The parameter 'version' is missing",
    3: "MISSING_JSON - The parameter 'json_data' is missing",
    4: "INVALID_JSON - The input structure is not valid JSON string",
    5: "MISSING_AUTH_USER - The parameter 'auth_user' is missing",
    6: "MISSING_AUTH_PWD - The parameter 'auth_pwd' is missing",
    10: "UNSUPPORTED_VERSION - No operation is available for the specified version",
    11: "UNKNOWN_OPERATION - The requested operation is not valid for the specified version",
    12: "UNSAFE - The requested operation cannot be performed because it can cause data (integrity) loss",
    100: "INTERNAL_ERROR - The operation could not be performed, see the message for troubleshooting",
}


class ItopError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        label = itop_error_codes.get(code, "UNKNOWN_ERROR - Not specified by iTop.")
        super().__init__(f"{message}\n{code} {label}")
