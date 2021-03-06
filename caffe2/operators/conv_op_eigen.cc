#include "caffe2/core/context.h"
#include "caffe2/core/operator.h"
#include "caffe2/operators/conv_pool_op_base.h"

#include "unsupported/Eigen/CXX11/Tensor"

namespace caffe2 {

template <typename T>
class EigenConvOp final : public ConvPoolOpBase<CPUContext> {
 public:
  USE_CONV_POOL_BASE_FUNCTIONS(CPUContext);
  EigenConvOp(const OperatorDef& operator_def, Workspace* ws)
      : ConvPoolOpBase<CPUContext>(operator_def, ws) {}
  ~EigenConvOp() {}

  bool RunOnDeviceWithOrderNCHW() override;
  bool RunOnDeviceWithOrderNHWC() override;

 private:
  INPUT_TAGS(INPUT, FILTER, BIAS);
};

// The NCHW implementation: we do explicit transposes before and after, which
// are not ideal but provides a compatible path instead of throwing the error.
template <typename T>
bool EigenConvOp<T>::RunOnDeviceWithOrderNCHW() {
  auto& X = Input(INPUT);
  auto& filter = Input(FILTER);
  auto& bias = Input(BIAS);
  auto* Y = Output(0);
  const int N = X.dim32(0), C = X.dim32(1), H = X.dim32(2), W = X.dim32(3);
  CAFFE_ENFORCE(4 == filter.ndim());
  const int M = filter.dim32(0);
  CAFFE_ENFORCE(filter.dim32(1) == C);
  CAFFE_ENFORCE(filter.dim32(2) == kernel_h_);
  CAFFE_ENFORCE(filter.dim32(3) == kernel_w_);
  CAFFE_ENFORCE(1 == bias.ndim());
  CAFFE_ENFORCE(bias.dim32(0) == M);
  ConvPoolOpBase<CPUContext>::SetOutputSize(X, Y, filter.dim32(0));
  Eigen::array<TIndex, 4> kernel_shuffles({2, 3, 1, 0});
  Eigen::array<TIndex, 4> input_shuffles({0, 2, 3, 1});

  Eigen::Tensor<T, 4, Eigen::RowMajor> filter_tensor =
      Eigen::TensorMap<Eigen::Tensor<T, 4, Eigen::RowMajor>>(
          const_cast<T*>(filter.template data<T>()), M, C, kernel_h_, kernel_w_)
          .shuffle(kernel_shuffles);
  Eigen::Tensor<T, 4, Eigen::RowMajor> X_tensor =
      Eigen::TensorMap<Eigen::Tensor<T, 4, Eigen::RowMajor>>(
          const_cast<T*>(X.template data<T>()), N, C, H, W)
          .shuffle(input_shuffles);

  // For Eigen, the definition of row and col actually correspond to width
  // and height instead of the other way round, so notice how we pass the
  // stride, pad and dilation values.
  typedef typename Eigen::internal::traits<
      Eigen::Tensor<T, 4, Eigen::RowMajor>>::Index TensorIndex;
  Eigen::array<Eigen::IndexPair<TensorIndex>, 1> contract_dims;
  contract_dims[0] = Eigen::IndexPair<TensorIndex>(1, 0);

  Eigen::DSizes<TensorIndex, 2> pre_contract_dims;
  pre_contract_dims[1] = kernel_h_ * kernel_w_ * C;
  pre_contract_dims[0] = Y->size() / M;

  Eigen::DSizes<TensorIndex, 2> kernel_dims;
  kernel_dims[0] = kernel_h_ * kernel_w_ * C;
  kernel_dims[1] = M;

  Eigen::array<TensorIndex, 4> bcast_dims;
  bcast_dims[0] = N;
  bcast_dims[1] = Y->dim32(1);
  bcast_dims[2] = Y->dim32(2);
  bcast_dims[3] = 1;

  Eigen::Tensor<T, 4, Eigen::RowMajor> Y_tensor(
      Y->dim32(0), Y->dim32(2), Y->dim32(3), Y->dim32(1));
  Y_tensor = X_tensor
                 .extract_image_patches(
                     kernel_w_,
                     kernel_h_,
                     stride_w_,
                     stride_h_,
                     dilation_w_,
                     dilation_h_,
                     1,
                     1,
                     pad_l_,
                     pad_r_,
                     pad_t_,
                     pad_b_,
                     0)
                 .reshape(pre_contract_dims)
                 .contract(filter_tensor.reshape(kernel_dims), contract_dims)
                 .reshape(Y_tensor.dimensions());
  //+ bias_tensor.broadcast(bcast_dims);
  // It seems that the bias broadcast above is still slower so let's do the
  // following for now.
  EigenArrayMap<T> Y_arr(
      Y_tensor.data(), static_cast<TIndex>(M), Y->size() / M);
  ConstEigenVectorArrayMap<T> bias_arr(bias.template data<T>(), M);
  Y_arr = Y_arr.colwise() + bias_arr;

  // Do a last transpose.
  Eigen::array<TIndex, 4> output_shuffles({0, 3, 1, 2});

  Eigen::TensorMap<Eigen::Tensor<T, 4, Eigen::RowMajor>>(
      Y->template mutable_data<T>(), N, M, Y->dim32(2), Y->dim32(3)) =
      Y_tensor.shuffle(output_shuffles);
  return true;
}

template <typename T>
bool EigenConvOp<T>::RunOnDeviceWithOrderNHWC() {
  auto& X = Input(INPUT);
  auto& filter = Input(FILTER);
  auto& bias = Input(BIAS);
  auto* Y = Output(0);
  const int N = X.dim32(0), H = X.dim32(1), W = X.dim32(2), C = X.dim32(3);
  CAFFE_ENFORCE(4 == filter.ndim());
  const int M = filter.dim32(0);
  CAFFE_ENFORCE(filter.dim32(1) == kernel_h_);
  CAFFE_ENFORCE(filter.dim32(2) == kernel_w_);
  CAFFE_ENFORCE(filter.dim32(3) == C);
  CAFFE_ENFORCE(1 == bias.ndim());
  CAFFE_ENFORCE(bias.dim32(0) == M);
  ConvPoolOpBase<CPUContext>::SetOutputSize(X, Y, filter.dim32(0));
  // Eigen expects filter to be of shape (kernel_h, kernel_w, C, M) for
  // optimization purposes, so we will create a temp one.
  Eigen::Array<T, Eigen::Dynamic, Eigen::Dynamic> temp_filter(
      M, kernel_h_ * kernel_w_ * C);
  temp_filter = ConstEigenArrayMap<T>(
                    filter.template data<T>(), kernel_h_ * kernel_w_ * C, M)
                    .transpose();

  // Create tensor maps, and call spatial convolution.
  // TODO(jiayq): right now we const cast away the const pointer, but we will
  // need to figure out how to properly do a const tensormap.
  Eigen::TensorMap<Eigen::Tensor<T, 4, Eigen::RowMajor>> X_tensor(
      const_cast<T*>(X.template data<T>()), N, H, W, C);
  Eigen::TensorMap<Eigen::Tensor<T, 4, Eigen::RowMajor>> Y_tensor(
      Y->template mutable_data<T>(), N, Y->dim32(1), Y->dim32(2), M);
  Eigen::TensorMap<Eigen::Tensor<T, 4, Eigen::RowMajor>> filter_tensor(
      const_cast<T*>(temp_filter.data()), kernel_h_, kernel_w_, C, M);
  Eigen::TensorMap<Eigen::Tensor<T, 4, Eigen::RowMajor>> bias_tensor(
      const_cast<T*>(bias.template data<T>()), 1, 1, 1, M);

  // For Eigen, the definition of row and col actually correspond to width
  // and height instead of the other way round, so notice how we pass the
  // stride, pad and dilation values.
  typedef typename Eigen::internal::traits<
      Eigen::Tensor<T, 4, Eigen::RowMajor>>::Index TensorIndex;
  Eigen::array<Eigen::IndexPair<TensorIndex>, 1> contract_dims;
  contract_dims[0] = Eigen::IndexPair<TensorIndex>(1, 0);

  Eigen::DSizes<TensorIndex, 2> pre_contract_dims;
  pre_contract_dims[1] = kernel_h_ * kernel_w_ * C;
  pre_contract_dims[0] = Y->size() / M;

  Eigen::DSizes<TensorIndex, 2> kernel_dims;
  kernel_dims[0] = kernel_h_ * kernel_w_ * C;
  kernel_dims[1] = M;

  Eigen::array<TensorIndex, 4> bcast_dims;
  bcast_dims[0] = N;
  bcast_dims[1] = Y->dim32(1);
  bcast_dims[2] = Y->dim32(2);
  bcast_dims[3] = 1;

  Y_tensor = X_tensor
                 .extract_image_patches(
                     kernel_w_,
                     kernel_h_,
                     stride_w_,
                     stride_h_,
                     dilation_w_,
                     dilation_h_,
                     1,
                     1,
                     pad_l_,
                     pad_r_,
                     pad_t_,
                     pad_b_,
                     0)
                 .reshape(pre_contract_dims)
                 .contract(filter_tensor.reshape(kernel_dims), contract_dims)
                 .reshape(Y_tensor.dimensions());
  //+ bias_tensor.broadcast(bcast_dims);
  // It seems that the bias broadcast above is still slower so let's do the
  // following for now.
  EigenArrayMap<T> Y_arr(
      Y->template mutable_data<T>(), static_cast<TIndex>(M), Y->size() / M);
  ConstEigenVectorArrayMap<T> bias_arr(bias.template data<T>(), M);
  Y_arr = Y_arr.colwise() + bias_arr;
  return true;
}

REGISTER_CPU_OPERATOR_WITH_ENGINE(Conv, EIGEN, EigenConvOp<float>);

} // namespace caffe2
