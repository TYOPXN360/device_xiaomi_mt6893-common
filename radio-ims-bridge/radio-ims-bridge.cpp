/*
 * Copyright (C) 2026
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Publishes the AIDL IMS radio service.
 *
 * android.hardware.radio-service.compat builds AIDL data/messaging/modem/
 * network/sim/voice on top of the HIDL radio HAL, but it never publishes
 * RadioIms, so the framework's check
 *
 *     RIL.isRadioServiceSupported(HAL_SERVICE_IMS)
 *       -> ServiceManager.isDeclared("android.hardware.radio.ims.IRadioIms/slot1")
 *
 * keeps failing and the framework logs "Feature android.hardware.telephony.ims
 * is declared, but service IMS is missing" on every phone.
 *
 * The MediaTek RIL exposes the IMS radio as a plain IRadio instance named
 * "imsAospSlotN" (android.hardware.radio@1.x::IRadio/imsAospSlot1), which is
 * exactly what compat::RadioIms wraps - it derives from RadioCompatBase, which
 * holds a sp<V1_5::IRadio>, not the older IOemRcsService. So we reuse the
 * AOSP compat implementation and only supply the publication that the stock
 * service is missing.
 */

#include <android-base/loging.h>
#include <android/binder_manager.h>
#include <android/binder_process.h>
#include <android/hardware/radio/1.5/IRadio.h>
#include <hidl/DeathRecipient.h>
#include <hidl/HidlTransportSupport.h>
#include <libradiocompat/CallbackManager.h>
#include <libradiocompat/DriverContext.h>
#include <libradiocompat/RadioIms.h>

#include <string>
#include <vector>

using android::hardware::radio::V1_5::IRadio;
using android::hardware::radio::compat::CallbackManager;
using android::hardware::radio::compat::DriverContext;
using android::hardware::radio::compat::RadioIms;
using ::android::OK;

namespace {

using namespace std::string_literals;

constexpr char kImsAidlDescriptor[] = "android.hardware.radio.ims.IRadioIms";
// AIDL instance the framework looks for.
constexpr char kSlot1[] = "slot1";
constexpr char kSlot2[] = "slot2";
// HIDL instance the MediaTek RIL registers the IMS radio under.
constexpr char kHidlImsSlot1[] = "imsAospSlot1";
constexpr char kHidlImsSlot2[] = "imsAospSlot2";

std::vector<std::shared_ptr<ndk::ICInterface>> gPublishedHals;

class DeathRecipient : public android::hidl::DeathRecipient {
  public:
    explicit DeathRecipient(std::string instance) : mInstance(std::move(instance)) {}
    void onDeath() override { LOG(ERROR) << "HIDL radio died for " << mInstance; }

  private:
    std::string mInstance;
};

std::map<std::string, android::hidl::DeathRecipient::DeathRecipient> gDeathRecipients;

void publishIms(const std::string& slot, const std::string& hidlInstance) {
    auto radioHidl = IRadio::getService(hidlInstance);
    if (!radioHidl) {
        LOG(WARNING) << "HIDL radio " << hidlInstance << " not present, skipping " << slot;
        return;
    }

    const std::string instance = std::string(kImsAidlDescriptor) + "/" + slot;
    if (!AServiceManager_isDeclared(instance.c_str())) {
        LOG(INFO) << instance << " is not declared in VINTF, skipping";
        return;
    }

    gDeathRecipients[hidlInstance] = ::new DeathRecipient(hidlInstance);
    android::hidl::linkToDeath(*radioHidl, gDeathRecipients[hidlInstance], true);

    auto context = std::make_shared<DriverContext>();
    auto callbackMgr = std::make_shared<CallbackManager>(context, radioHidl);

    auto aidlHal = ndk::SharedRefBase::make<RadioIms>(context, radioHidl, callbackMgr);
    gPublishedHals.push_back(aidlHal);

    const ::binder::Status status = AServiceManager_addService(aidlHal->asBinder().get(),
                                                             instance.c_str());
    if (status != OK) {
        LOG(ERROR) << "Failed to publish " << instance << ": " << status;
        return;
    }
    LOG(INFO) << "Published " << instance << " on top of " << hidlInstance;
}

}  // namespace

int main(int argc, char* argv[]) {
    android::base::SetDefaultTag("radioimsbridge");
    android::base::SetMinimumLogSeverity(android::base::VERBOSE);

    LOG(INFO) << "IMS radio bridge starting";

    publishIms(kSlot1, kHidlImsSlot1);
    publishIms(kSlot2, kHidlImsSlot2);

    LOG(INFO) << "IMS radio bridge operational";
    ABinderProcess_joinThreadPool();
    LOG(FATAL) << "IMS radio bridge has stopped";
    return EXIT_FAILURE;
}
