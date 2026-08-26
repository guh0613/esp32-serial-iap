#pragma once

#include "bootloader_config.h"

void iap_bootloader_run(const bootloader_state_t *bootloader_state,
                        int current_boot_index);
