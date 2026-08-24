Domain: Embedded C++ (Arduino/ESP32).
Criteria hints: no dynamic allocation in the hot loop unless justified,
explicit handling of sensor/peripheral failure (don't assume a read always
succeeds), and state made explicit (a real state machine, not scattered
booleans) if the task involves more than one mode of operation.
