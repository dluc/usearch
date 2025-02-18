/**
 *  @file javascript.cpp
 *  @author Ash Vardanian
 *  @brief JavaScript bindings for Unum USearch.
 *  @date 2023-04-26
 *
 *  @copyright Copyright (c) 2023
 *
 *  @see NodeJS docs: https://nodejs.org/api/addons.html#hello-world
 *
 */
#include <new> // `std::bad_alloc`

#define NAPI_CPP_EXCEPTIONS
#include <napi.h>
#include <node_api.h>

#include <usearch/index_dense.hpp>

using namespace unum::usearch;
using namespace unum;

class Index : public Napi::ObjectWrap<Index> {
  public:
    static Napi::Object Init(Napi::Env env, Napi::Object exports);
    Index(Napi::CallbackInfo const& ctx);

  private:
    Napi::Value GetDimensions(Napi::CallbackInfo const& ctx);
    Napi::Value GetSize(Napi::CallbackInfo const& ctx);
    Napi::Value GetCapacity(Napi::CallbackInfo const& ctx);
    Napi::Value GetConnectivity(Napi::CallbackInfo const& ctx);

    void Save(Napi::CallbackInfo const& ctx);
    void Load(Napi::CallbackInfo const& ctx);
    void View(Napi::CallbackInfo const& ctx);

    void Add(Napi::CallbackInfo const& ctx);
    Napi::Value Search(Napi::CallbackInfo const& ctx);
    Napi::Value Remove(Napi::CallbackInfo const& ctx);
    Napi::Value Contains(Napi::CallbackInfo const& ctx);

    std::unique_ptr<index_dense_t> native_;
};

Napi::Object Index::Init(Napi::Env env, Napi::Object exports) {
    Napi::Function func = DefineClass( //
        env, "Index",
        {
            InstanceMethod("dimensions", &Index::GetDimensions),
            InstanceMethod("size", &Index::GetSize),
            InstanceMethod("capacity", &Index::GetCapacity),
            InstanceMethod("connectivity", &Index::GetConnectivity),
            InstanceMethod("add", &Index::Add),
            InstanceMethod("search", &Index::Search),
            InstanceMethod("remove", &Index::Remove),
            InstanceMethod("contains", &Index::Contains),
            InstanceMethod("save", &Index::Save),
            InstanceMethod("load", &Index::Load),
            InstanceMethod("view", &Index::View),
        });

    Napi::FunctionReference* constructor = new Napi::FunctionReference();
    *constructor = Napi::Persistent(func);
    env.SetInstanceData(constructor);

    exports.Set("Index", func);
    return exports;
}

Index::Index(Napi::CallbackInfo const& ctx) : Napi::ObjectWrap<Index>(ctx) {
    Napi::Env env = ctx.Env();

    int length = ctx.Length();
    if (length == 0 || length >= 2 || !ctx[0].IsObject()) {
        Napi::TypeError::New(env, "Pass args as named objects: dimensions: uint, capacity: uint, metric: str")
            .ThrowAsJavaScriptException();
        return;
    }

    bool lossless = true;
    Napi::Object params = ctx[0].As<Napi::Object>();
    std::size_t dimensions =
        params.Has("dimensions") ? params.Get("dimensions").As<Napi::BigInt>().Uint64Value(&lossless) : 0;

    index_limits_t limits;
    std::size_t connectivity = default_connectivity();
    std::size_t expansion_add = default_expansion_add();
    std::size_t expansion_search = default_expansion_search();

    if (params.Has("capacity"))
        limits.members = params.Get("capacity").As<Napi::BigInt>().Uint64Value(&lossless);
    if (params.Has("connectivity"))
        connectivity = params.Get("connectivity").As<Napi::BigInt>().Uint64Value(&lossless);
    if (params.Has("expansion_add"))
        expansion_add = params.Get("expansion_add").As<Napi::BigInt>().Uint64Value(&lossless);
    if (params.Has("expansion_search"))
        expansion_search = params.Get("expansion_search").As<Napi::BigInt>().Uint64Value(&lossless);
    if (!lossless) {
        Napi::TypeError::New(env, "Arguments must be unsigned integers").ThrowAsJavaScriptException();
        return;
    }

    scalar_kind_t quantization = scalar_kind_t::f32_k;
    if (params.Has("quantization")) {
        std::string quantization_str = params.Get("quantization").As<Napi::String>().Utf8Value();
        expected_gt<scalar_kind_t> expected = scalar_kind_from_name(quantization_str.c_str(), quantization_str.size());
        if (!expected) {
            Napi::TypeError::New(env, expected.error.release()).ThrowAsJavaScriptException();
            return;
        }
        quantization = *expected;
    }

    // By default we use the Inner Product similarity
    metric_kind_t metric_kind = metric_kind_t::ip_k;
    if (params.Has("metric")) {
        std::string metric_str = params.Get("metric").As<Napi::String>().Utf8Value();
        expected_gt<metric_kind_t> expected = metric_from_name(metric_str.c_str(), metric_str.size());
        if (!expected) {
            Napi::TypeError::New(env, expected.error.release()).ThrowAsJavaScriptException();
            return;
        }
        metric_kind = *expected;
    }

    metric_punned_t metric(dimensions, metric_kind, quantization);
    index_dense_config_t config(connectivity, expansion_add, expansion_search);
    native_.reset(new index_dense_t(index_dense_t::make(metric, config)));
    native_->reserve(limits);
}

Napi::Value Index::GetDimensions(Napi::CallbackInfo const& ctx) {
    return Napi::BigInt::New(ctx.Env(), static_cast<std::uint64_t>(native_->dimensions()));
}
Napi::Value Index::GetSize(Napi::CallbackInfo const& ctx) {
    return Napi::BigInt::New(ctx.Env(), static_cast<std::uint64_t>(native_->size()));
}
Napi::Value Index::GetConnectivity(Napi::CallbackInfo const& ctx) {
    return Napi::BigInt::New(ctx.Env(), static_cast<std::uint64_t>(native_->connectivity()));
}
Napi::Value Index::GetCapacity(Napi::CallbackInfo const& ctx) {
    return Napi::BigInt::New(ctx.Env(), static_cast<std::uint64_t>(native_->capacity()));
}

void Index::Save(Napi::CallbackInfo const& ctx) {
    Napi::Env env = ctx.Env();

    int length = ctx.Length();
    if (length == 0 || !ctx[0].IsString()) {
        Napi::TypeError::New(env, "Function expects a string path argument").ThrowAsJavaScriptException();
        return;
    }

    try {
        std::string path = ctx[0].As<Napi::String>();
        auto result = native_->save(path.c_str());
        if (!result)
            return Napi::TypeError::New(env, result.error.release()).ThrowAsJavaScriptException();

    } catch (...) {
        Napi::TypeError::New(env, "Serialization failed").ThrowAsJavaScriptException();
    }
}

void Index::Load(Napi::CallbackInfo const& ctx) {
    Napi::Env env = ctx.Env();

    int length = ctx.Length();
    if (length == 0 || !ctx[0].IsString()) {
        Napi::TypeError::New(env, "Function expects a string path argument").ThrowAsJavaScriptException();
        return;
    }

    try {
        std::string path = ctx[0].As<Napi::String>();
        auto result = native_->load(path.c_str());
        if (!result)
            return Napi::TypeError::New(env, result.error.release()).ThrowAsJavaScriptException();

    } catch (...) {
        Napi::TypeError::New(env, "Loading failed").ThrowAsJavaScriptException();
    }
}

void Index::View(Napi::CallbackInfo const& ctx) {
    Napi::Env env = ctx.Env();

    int length = ctx.Length();
    if (length == 0 || !ctx[0].IsString()) {
        Napi::TypeError::New(env, "Function expects a string path argument").ThrowAsJavaScriptException();
        return;
    }

    try {
        std::string path = ctx[0].As<Napi::String>();
        auto result = native_->view(path.c_str());
        if (!result)
            return Napi::TypeError::New(env, result.error.release()).ThrowAsJavaScriptException();

    } catch (...) {
        Napi::TypeError::New(env, "Memory-mapping failed").ThrowAsJavaScriptException();
    }
}

void Index::Add(Napi::CallbackInfo const& ctx) {
    Napi::Env env = ctx.Env();

    if (ctx.Length() < 2)
        return Napi::TypeError::New(env, "Expects at least two arguments").ThrowAsJavaScriptException();

    using key_t = typename index_dense_t::key_t;
    std::size_t index_dimensions = native_->dimensions();

    auto add = [&](Napi::BigInt key_js, Napi::Float32Array vector_js) {
        bool lossless = true;
        key_t key = static_cast<key_t>(key_js.Uint64Value(&lossless));
        if (!lossless)
            return Napi::TypeError::New(env, "Keys must be unsigned integers").ThrowAsJavaScriptException();

        float const* vector = vector_js.Data();
        std::size_t dimensions = static_cast<std::size_t>(vector_js.ElementLength());

        if (dimensions != index_dimensions)
            return Napi::TypeError::New(env, "Wrong number of dimensions").ThrowAsJavaScriptException();

        try {
            auto result = native_->add(key, vector);
            if (!result)
                return Napi::TypeError::New(env, result.error.release()).ThrowAsJavaScriptException();

        } catch (std::bad_alloc const&) {
            return Napi::TypeError::New(env, "Out of memory").ThrowAsJavaScriptException();
        } catch (...) {
            return Napi::TypeError::New(env, "Insertion failed").ThrowAsJavaScriptException();
        }
    };

    if (ctx[0].IsArray() && ctx[1].IsArray()) {
        Napi::Array keys_js = ctx[0].As<Napi::Array>();
        Napi::Array vectors_js = ctx[1].As<Napi::Array>();
        auto length = keys_js.Length();

        if (length != vectors_js.Length())
            return Napi::TypeError::New(env, "The number of keys must match the number of vectors")
                .ThrowAsJavaScriptException();

        if (native_->size() + length >= native_->capacity())
            if (!native_->reserve(ceil2(native_->size() + length)))
                return Napi::TypeError::New(env, "Out of memory!").ThrowAsJavaScriptException();

        for (std::size_t i = 0; i < length; i++) {
            Napi::Value key_js = keys_js[i];
            Napi::Value vector_js = vectors_js[i];
            add(key_js.As<Napi::BigInt>(), vector_js.As<Napi::Float32Array>());
        }

    } else if (ctx[0].IsBigInt() && ctx[1].IsTypedArray()) {
        if (native_->size() + 1 >= native_->capacity())
            native_->reserve(ceil2(native_->size() + 1));
        add(ctx[0].As<Napi::BigInt>(), ctx[1].As<Napi::Float32Array>());
    } else
        return Napi::TypeError::New(env, "Invalid argument type, expects integral key(s) and float vector(s)")
            .ThrowAsJavaScriptException();
}

Napi::Value Index::Search(Napi::CallbackInfo const& ctx) {
    Napi::Env env = ctx.Env();
    if (ctx.Length() < 2 || !ctx[0].IsTypedArray() || !ctx[1].IsBigInt()) {
        Napi::TypeError::New(env, "Expects a  and the number of wanted results").ThrowAsJavaScriptException();
        return {};
    }

    Napi::Float32Array vector_js = ctx[0].As<Napi::Float32Array>();
    Napi::BigInt wanted_js = ctx[1].As<Napi::BigInt>();

    float const* vector = vector_js.Data();
    std::size_t dimensions = static_cast<std::size_t>(vector_js.ElementLength());
    if (dimensions != native_->dimensions()) {
        Napi::TypeError::New(env, "Wrong number of dimensions").ThrowAsJavaScriptException();
        return {};
    }

    bool lossless = true;
    std::uint64_t wanted = wanted_js.Uint64Value(&lossless);
    if (!lossless) {
        Napi::TypeError::New(env, "Wanted number of matches must be an unsigned integer").ThrowAsJavaScriptException();
        return {};
    }

    using key_t = typename index_dense_t::key_t;
    Napi::TypedArrayOf<key_t> matches_js = Napi::TypedArrayOf<key_t>::New(env, wanted);
    static_assert(std::is_same<std::uint64_t, key_t>::value, "Matches.key interface expects BigUint64Array");
    Napi::Float32Array distances_js = Napi::Float32Array::New(env, wanted);
    try {

        auto result = native_->search(vector, wanted);
        if (!result) {
            Napi::TypeError::New(env, result.error.release()).ThrowAsJavaScriptException();
            return {};
        }

        std::uint64_t count = result.dump_to(matches_js.Data(), distances_js.Data());
        Napi::Object result_js = Napi::Object::New(env);
        result_js.Set("keys", matches_js);
        result_js.Set("distances", distances_js);
        result_js.Set("count", Napi::BigInt::New(env, count));
        return result_js;
    } catch (std::bad_alloc const&) {
        Napi::TypeError::New(env, "Out of memory").ThrowAsJavaScriptException();
        return {};
    } catch (...) {
        Napi::TypeError::New(env, "Search failed").ThrowAsJavaScriptException();
        return {};
    }
}

Napi::Value Index::Remove(Napi::CallbackInfo const& ctx) {
    Napi::Env env = ctx.Env();
    if (ctx.Length() < 1 || !ctx[0].IsBigInt()) {
        Napi::TypeError::New(env, "Expects an entry identifier").ThrowAsJavaScriptException();
        return {};
    }

    Napi::BigInt key_js = ctx[0].As<Napi::BigInt>();
    bool lossless = true;
    std::uint64_t key = key_js.Uint64Value(&lossless);
    if (!lossless) {
        Napi::TypeError::New(env, "Identifier must be an unsigned integer").ThrowAsJavaScriptException();
        return {};
    }

    try {
        auto result = native_->remove(key);
        if (!result) {
            Napi::TypeError::New(env, result.error.release()).ThrowAsJavaScriptException();
            return {};
        }
        return Napi::Boolean::New(env, result.completed);
    } catch (std::bad_alloc const&) {
        Napi::TypeError::New(env, "Out of memory").ThrowAsJavaScriptException();
        return {};
    } catch (...) {
        Napi::TypeError::New(env, "Search failed").ThrowAsJavaScriptException();
        return {};
    }
}

Napi::Value Index::Contains(Napi::CallbackInfo const& ctx) {
    Napi::Env env = ctx.Env();
    if (ctx.Length() < 1 || !ctx[0].IsBigInt()) {
        Napi::TypeError::New(env, "Expects an entry identifier").ThrowAsJavaScriptException();
        return {};
    }

    Napi::BigInt key_js = ctx[0].As<Napi::BigInt>();
    bool lossless = true;
    std::uint64_t key = key_js.Uint64Value(&lossless);
    if (!lossless) {
        Napi::TypeError::New(env, "Identifier must be an unsigned integer").ThrowAsJavaScriptException();
        return {};
    }

    try {
        bool result = native_->contains(key);
        return Napi::Boolean::New(env, result);
    } catch (std::bad_alloc const&) {
        Napi::TypeError::New(env, "Out of memory").ThrowAsJavaScriptException();
        return {};
    } catch (...) {
        Napi::TypeError::New(env, "Search failed").ThrowAsJavaScriptException();
        return {};
    }
}

Napi::Object InitAll(Napi::Env env, Napi::Object exports) { return Index::Init(env, exports); }

NODE_API_MODULE(usearch, InitAll)
