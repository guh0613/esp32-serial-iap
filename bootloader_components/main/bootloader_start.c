/*
 * SPDX-FileCopyrightText: 2015-2024 Espressif Systems (Shanghai) CO LTD
 * SPDX-License-Identifier: Apache-2.0
 *
 * Derived from ESP-IDF v5.5.5 components/bootloader/subproject/main/
 * bootloader_start.c. Keep changes minimal so upstream behavior is easy to
 * compare when ESP-IDF is upgraded.
 */

#include <stdbool.h>
#include "sdkconfig.h"

#if CONFIG_LIBC_NEWLIB
#include <sys/reent.h>
#endif

#include "bootloader_init.h"
#include "bootloader_utility.h"
#include "esp_log.h"
#include "iap_bootloader.h"

void __attribute__((weak)) bootloader_before_init(void);
void __attribute__((weak)) bootloader_after_init(void);

#if CONFIG_BOOTLOADER_FACTORY_RESET
#error "Custom Serial IAP bootloader does not yet preserve GPIO factory reset"
#endif

#if CONFIG_BOOTLOADER_APP_TEST
#error "Custom Serial IAP bootloader does not yet preserve GPIO app-test selection"
#endif

static const char *TAG = "boot";

void __attribute__((noreturn)) call_start_cpu0(void)
{
    if (bootloader_before_init) {
        bootloader_before_init();
    }

    if (bootloader_init() != ESP_OK) {
        bootloader_reset();
    }

    if (bootloader_after_init) {
        bootloader_after_init();
    }

#ifdef CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP
    bootloader_utility_load_boot_image_from_deep_sleep();
#endif

    bootloader_state_t bootloader_state = {0};
    if (!bootloader_utility_load_partition_table(&bootloader_state)) {
        ESP_LOGE(TAG, "load partition table error!");
        bootloader_reset();
    }

    int boot_index = bootloader_utility_get_selected_boot_partition(
        &bootloader_state);
    if (boot_index == INVALID_INDEX) {
        bootloader_reset();
    }

    iap_bootloader_run(&bootloader_state, boot_index);

    // IAP may have returned after ABORT/BOOT/timeout. Re-read otadata rather
    // than relying on a selection made before the serial session.
    boot_index = bootloader_utility_get_selected_boot_partition(
        &bootloader_state);
    if (boot_index == INVALID_INDEX) {
        bootloader_reset();
    }

#if CONFIG_SECURE_ENABLE_TEE
    bootloader_utility_load_tee_image(&bootloader_state);
#endif

    bootloader_utility_load_boot_image(&bootloader_state, boot_index);
}

#if CONFIG_LIBC_NEWLIB
struct _reent *__getreent(void)
{
    return _GLOBAL_REENT;
}
#endif
